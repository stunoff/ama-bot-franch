"""Versioned publications and per-recipient obligations (§12). Caller owns transaction."""

from copy import deepcopy
from types import SimpleNamespace
from datetime import timedelta, datetime, timezone
from calendar import monthrange
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit
import json
from sqlalchemy import select
from ama.auth import Actor, active_person_role
from ama.config import settings
from ama.errors import BusinessError, AccessDenied
from ama.models import (
    Account,
    Packet,
    PacketRevision,
    PacketPublication,
    PacketObligation,
    PacketQuestion,
    PacketAsset,
    PacketSchedule,
    Entity,
    EstablishmentBrand,
    Binding,
    Role,
    Record,
    ProjectPlan,
    Work,
    Outbox,
    Attachment,
    now,
)
from ama.calendar import Calendar
from ama.service import queue, stamp, versioned, parse_date, STAFF
from ama.ui import access

MODES = {"information", "acknowledgement", "task"}
SCOPES = {"partner", "partner_brand", "facility", "facility_brand"}


def publisher(db, actor, obj=None, general=False):
    b = db.get(Binding, actor.binding_id)
    if (
        not b
        or b.bot_id != settings().bot_id
        or not b.active
        or not b.verified
        or b.purpose not in {"support", "marketing"}
        or b.platform != "telegram"
    ):
        raise AccessDenied("Рассылки создаются в сопровождении или маркетинге.")
    names = {"curator", "support_head"} if general else {"curator", "support_head", "marketing_manager"}
    if not (
        access.scope(db, actor, obj, names)
        if obj
        else any(r.name in names and access.staff_role(db, actor, r.name) for r in access.assignments(db, actor))
    ):
        raise AccessDenied("Нет бизнес-роли в области рассылки.")
    grants = []
    for r in db.scalars(select(Record).where(Record.kind == "policy", Record.published.is_(True))):
        d = r.data
        if (
            d.get("policy_type") != "broadcast_permission"
            or d.get("person_id") != actor.person_id
            or not d.get("approval")
        ):
            continue
        if general and not d.get("allow_general"):
            continue
        if obj and (
            any(d.get(k) and d[k] != getattr(obj, k, None) for k in ("partner_id", "brand_id", "establishment_id"))
            or d.get("brand_ids")
            and getattr(obj, "brand_id", None) not in d["brand_ids"]
        ):
            continue
        grants.append({"id": r.id, "version": r.version})
    if not grants:
        raise AccessDenied("Администратор должен опубликовать право рассылки и разрешённую аудиторию.")
    return grants


def get(db, packet_id, lock=False):
    q = select(Packet).where(Packet.id == packet_id, Packet.bot_id == settings().bot_id)
    p = db.scalar(q.with_for_update().execution_options(populate_existing=True) if lock else q)
    if not p:
        raise BusinessError("Пакет не найден.")
    return p


def revision(db, p, number=None):
    r = db.scalar(
        select(PacketRevision).where(
            PacketRevision.packet_id == p.id, PacketRevision.number == (number or p.current_revision)
        )
    )
    if not r:
        raise BusinessError("Редакция не найдена.")
    return r


def is_author(db, actor, p):
    return p.author_id == actor.person_id and bool(publisher(db, actor))


def state_of(db, e, brand_id):
    plans = list(
        db.scalars(
            select(ProjectPlan)
            .where(
                ProjectPlan.bot_id == settings().bot_id,
                ProjectPlan.establishment_id == e.id,
                ProjectPlan.brand_id == brand_id,
            )
            .order_by(ProjectPlan.created_at.desc())
        )
    )
    if plans:
        p = plans[0]
        if (
            p.dates.get("actual_technical")
            or p.stage in {"technical", "enhanced_support", "ongoing"}
            or p.scenario in {"existing", "technical"}
        ):
            return "operating"
        if p.scenario != "reconstruction":
            return "starting"
        if any(old.dates.get("actual_technical") or old.scenario in {"existing", "technical"} for old in plans[1:]):
            return "operating"
    value = e.data.get("lifecycle")
    return value if value in {"operating", "starting"} else "unknown"


def assignment(db, partner, e, brand_id):
    scope = SimpleNamespace(partner_id=partner.id, establishment_id=e.id, brand_id=brand_id)
    plan = db.scalar(
        select(ProjectPlan)
        .where(
            ProjectPlan.bot_id == settings().bot_id,
            ProjectPlan.establishment_id == e.id,
            ProjectPlan.brand_id == brand_id,
        )
        .order_by(ProjectPlan.created_at.desc())
        .limit(1)
    )
    fp = partner.data.get("partner_person_id") or (plan.partner_person_id if plan else None)
    curator = e.data.get("curator_id") or (plan.curator_id if plan else None) or partner.data.get("curator_id")
    if not fp:
        people = {
            r.person_id
            for r in db.scalars(
                select(Role).where(Role.bot_id == settings().bot_id, Role.name == "partner", Role.active.is_(True))
            )
            if (not r.expires_at or r.expires_at > now()) and access.matches(r, scope)
        }
        if len(people) == 1:
            fp = next(iter(people))
    if not fp or not active_person_role(db, fp, {"partner"}, scope):
        raise BusinessError("Не назначен однозначный действующий ФП.")
    if not curator or not active_person_role(db, curator, {"curator", "support_head"}, scope):
        raise BusinessError("Не назначен закреплённый куратор предприятия.")
    return fp, curator


def materials(db, data, actor, approved_assets=None):
    result = []
    for key in data.get("material_keys", []):
        r = db.scalar(select(Record).where(Record.kind == "material", Record.key == key, Record.published.is_(True)))
        if not r or "partner" not in r.data.get("audience", []):
            raise BusinessError("Материал не опубликован для ФП: " + key)
        result.append(
            {
                "id": r.id,
                "key": key,
                "version": r.version,
                "title": r.data.get("title", key),
                "url": r.data["url"],
                "scope": {k: r.data.get(k) for k in ("partner_id", "brand_id", "establishment_id")},
            }
        )
    for link in data.get("links", []):
        u = urlsplit(link.get("url", ""))
        if u.scheme != "https" or not u.hostname or u.username or u.password or not link.get("title"):
            raise BusinessError("Нужны название и HTTPS-ссылка без пароля; видео публикуется внешней ссылкой.")
    assets = []
    for ident in data.get("asset_ids", []):
        a = db.get(PacketAsset, ident)
        if (
            not a
            or (
                a.author_id != actor.person_id
                and not any(x["id"] == a.id and x["sha256"] == a.sha256 for x in (approved_assets or []))
            )
            or not db.get(Binding, a.binding_id)
            or db.get(Binding, a.binding_id).bot_id != settings().bot_id
            or db.get(Binding, a.binding_id).purpose == "client"
            or a.status != "stored"
            or not a.sha256
            or not a.storage_path
        ):
            raise BusinessError("Файл пакета не сохранён или принадлежит другому автору/чату.")
        assets.append({"id": a.id, "sha256": a.sha256, "filename": a.filename})
    return result, assets


def validate_data(data):
    if (
        data.get("mode") not in MODES
        or data.get("scope") not in SCOPES
        or data.get("audience") not in {"brands", "general"}
    ):
        raise BusinessError("Выберите режим, область обязательства и аудиторию.")
    if not data.get("title") or not data.get("text") or len(data["title"]) > 250 or len(data["text"]) > 2500:
        raise BusinessError("Нужны название и текст пакета до 2500 символов.")
    if not data.get("apply_at"):
        raise BusinessError("Укажите дату применения изменений.")
    apply_at = parse_date(data["apply_at"])
    if data["audience"] == "brands" and not data.get("brand_ids"):
        raise BusinessError("Выберите бренды.")
    if data["mode"] == "task" and data["scope"] not in {"facility", "facility_brand"}:
        raise BusinessError("Исполнение принимается отдельно по физическому объекту или объекту и бренду.")
    if data["mode"] == "acknowledgement" and data["scope"] not in {"partner", "partner_brand"}:
        raise BusinessError("Для ознакомления выберите ФП или ФП + бренд.")
    if data["audience"] == "general" and data["mode"] == "acknowledgement" and data["scope"] != "partner":
        raise BusinessError("Общее ознакомление без бренда имеет одно обязательство на ФП.")
    if data["audience"] == "brands" and data["scope"] == "partner":
        raise BusinessError("Общее ознакомление ФП без бренда оформляется общей информацией УК.")
    if data["mode"] != "information":
        if not data.get("due_at") or parse_date(data["due_at"]) <= now() or parse_date(data["due_at"]) < apply_at:
            raise BusinessError("Нужен будущий срок исполнения не раньше даты применения.")
        if data["mode"] == "task" and not data.get("checklist"):
            raise BusinessError("Задание требует применимый чек-лист и ожидаемый результат.")
        for k in ("remind_days", "escalate_days"):
            n = data.get(k)
            if not isinstance(n, int) or isinstance(n, bool) or not 0 <= n <= 365:
                raise BusinessError(
                    "Укажите согласованные интервалы напоминания и эскалации в рабочих днях (0 — отключено)."
                )
    if data.get("population", "all") not in {"all", "operating", "starting"}:
        raise BusinessError("Неизвестный фильтр состояния предприятий.")


def audience(db, actor, data, approved_assets=None):
    validate_data(data)
    general = data["audience"] == "general"
    grants = publisher(db, actor, general=general)
    mats, assets = materials(db, data, actor, approved_assets)
    included = []
    excluded = []
    links = list(
        db.scalars(
            select(EstablishmentBrand)
            .where(EstablishmentBrand.active.is_(True))
            .order_by(EstablishmentBrand.establishment_id, EstablishmentBrand.brand_id)
        )
    )
    for link in links:
        e = db.get(Entity, link.establishment_id)
        partner = db.get(Entity, e.partner_id) if e else None
        if not e or not partner or e.data.get("archived") or partner.data.get("archived"):
            continue
        if not general and link.brand_id not in data["brand_ids"]:
            continue
        if data.get("establishment_ids") and e.id not in data["establishment_ids"]:
            continue
        scope = SimpleNamespace(partner_id=partner.id, establishment_id=e.id, brand_id=link.brand_id)
        # Exclude unauthorised rows without disclosing their details in the preview.
        try:
            publisher(db, actor, scope, general)
        except AccessDenied:
            continue
        reason = None
        state = state_of(db, e, link.brand_id)
        if data.get("population", "all") != "all" and state != data["population"]:
            reason = "Состояние предприятия: " + state
        for key in ("cities", "formats"):
            field = "city" if key == "cities" else "format"
            if data.get(key) and e.data.get(field) not in data[key]:
                reason = "Не соответствует фильтру " + field + " или значение не заполнено"
        if data.get("call_center", "all") != "all" and e.data.get("call_center_applicable") is not (
            data["call_center"] == "yes"
        ):
            reason = "Не соответствует применимости КЦ или она неизвестна"
        binding = db.scalar(
            select(Binding).where(
                Binding.bot_id == settings().bot_id,
                Binding.purpose == "client",
                Binding.partner_id == partner.id,
                Binding.brand_id == link.brand_id,
                Binding.active.is_(True),
                Binding.verified.is_(True),
            )
        )
        if not binding or binding.establishment_id not in {None, e.id}:
            reason = "Нет активного проверенного брендового чата"
        if not partner.data.get("notification_window"):
            reason = "Не согласовано окно уведомлений"
        else:
            Calendar(partner.data["notification_window"])
        try:
            fp, curator = assignment(db, partner, e, link.brand_id)
            if binding and not db.scalar(
                select(Account.id).where(
                    Account.person_id == fp, Account.platform == binding.platform, Account.active.is_(True)
                )
            ):
                reason = "Не подключён аккаунт уполномоченного ФП для платформы чата"
        except BusinessError as error:
            fp = curator = None
            reason = str(error)
        for material in mats:
            if any(v and v != getattr(scope, k) for k, v in material["scope"].items()):
                reason = "Материал не предназначен этой области: " + material["key"]
        item = {
            "partner_id": partner.id,
            "establishment_id": e.id,
            "brand_id": link.brand_id,
            "name": e.name,
            "binding_id": binding.id if binding else None,
            "binding_version": binding.version if binding else None,
            "person_id": fp,
            "curator_id": curator,
            "state": state,
            "versions": {"establishment": e.version, "partner": partner.version, "link": link.version},
        }
        if reason:
            excluded.append({**item, "reason": reason})
        else:
            included.append(item)
    if not included:
        raise BusinessError("Нет подходящих получателей: " + "; ".join(sorted({i["reason"] for i in excluded}))[:1200])
    if data["scope"] == "facility" and data["mode"] == "task":
        for est in {i["establishment_id"] for i in included}:
            group = [i for i in included if i["establishment_id"] == est]
            if len({i["person_id"] for i in group}) != 1 or len({i["curator_id"] for i in group}) != 1:
                raise BusinessError("Для общей физической работы нужен один ФП и один закреплённый куратор площадки.")
            scope = SimpleNamespace(partner_id=group[0]["partner_id"], establishment_id=est, brand_id=None)
            if not active_person_role(db, group[0]["curator_id"], {"curator", "support_head"}, scope):
                raise BusinessError(
                    "Куратор общей физической работы должен иметь назначение на площадку без ограничения одним брендом."
                )
    if data["scope"] == "partner" and data["mode"] == "acknowledgement":
        for partner_id in {i["partner_id"] for i in included}:
            if len({i["person_id"] for i in included if i["partner_id"] == partner_id}) != 1:
                raise BusinessError("Для общего ознакомления назначьте одного уполномоченного ФП для партнёра.")
    calendar = db.scalar(
        select(Record).where(
            Record.kind == "calendar", Record.key == data.get("calendar", "support"), Record.published.is_(True)
        )
    )
    if data["mode"] != "information" and not calendar:
        raise BusinessError("Не опубликован календарь контроля обязательств.")
    snapshot = {
        "included": included,
        "excluded": excluded,
        "materials": mats,
        "assets": assets,
        "permission": grants,
        "calendar": deepcopy(calendar.data) if calendar else None,
        "calendar_version": calendar.version if calendar else None,
        "author_grants": access.grants_hash(db, actor),
    }
    snapshot["fingerprint"] = digest(snapshot)
    return snapshot


def digest(data):
    return sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def scope_key(data, item):
    parts = [data["scope"], item["partner_id"]]
    if data["scope"] in {"facility", "facility_brand"}:
        parts.append(item["establishment_id"])
    if data["scope"] in {"partner_brand", "facility_brand"}:
        parts.append(item["brand_id"])
    return ":".join(parts)


def support_route(db, item):
    configured = db.scalar(
        select(Record).where(Record.kind == "route", Record.key == "support", Record.published.is_(True))
    )
    b = db.get(Binding, configured.data.get("binding_id")) if configured and configured.data.get("binding_id") else None
    if (
        b
        and b.bot_id == settings().bot_id
        and b.purpose == "support"
        and b.active
        and b.verified
        and all(
            not getattr(b, k) or getattr(b, k) == item.get(k) for k in ("partner_id", "brand_id", "establishment_id")
        )
    ):
        return b
    raise BusinessError("Не опубликован подходящий проверенный маршрут сопровождения для закреплённого куратора.")


def create(db, actor, data, key):
    old = db.get(Packet, key)
    if old:
        publisher(db, actor)
        if old.author_id != actor.person_id:
            raise AccessDenied("Пакет принадлежит другому автору.")
        return summary(db, old)
    snap = audience(db, actor, data)
    p = Packet(
        id=key,
        bot_id=settings().bot_id,
        author_id=actor.person_id,
        source_binding_id=actor.binding_id,
        title=data["title"],
        current_revision=1,
        version=1,
    )
    db.add(p)
    db.flush()
    r = PacketRevision(
        packet_id=p.id, number=1, data={**deepcopy(data), "author_binding": actor.binding_id}, audience=snap
    )
    db.add(r)
    db.flush()
    make_recipients(db, p, r, snap["included"], sender=actor)
    announce(db, p)
    stamp(db, actor, p, "PACKET_PUBLISHED", data={"revision": r.id, "fingerprint": snap["fingerprint"]})
    return {**summary(db, p), "fingerprint": snap["fingerprint"], "audience": snap}


def make_recipients(db, p, r, items, *, carry=None, sender=None):
    grouped = {}
    if r.data["mode"] != "information":
        for item in items:
            key = scope_key(r.data, item)
            grouped.setdefault(key, []).append(item)
    for key, group in grouped.items():
        item = group[0]
        old = db.scalar(
            select(PacketObligation).where(PacketObligation.revision_id == r.id, PacketObligation.scope_key == key)
        )
        if old:
            continue
        due_at = parse_date(r.data["due_at"])
        ob = PacketObligation(
            revision_id=r.id,
            scope_key=key,
            partner_id=item["partner_id"],
            establishment_id=item["establishment_id"] if r.data["scope"].startswith("facility") else None,
            brand_id=item["brand_id"] if r.data["scope"].endswith("brand") else None,
            person_id=item["person_id"],
            curator_id=item["curator_id"],
            binding_id=item["binding_id"],
            due_at=due_at,
            next_at=due_at,
            version=1,
            data={
                "brands": sorted({i["brand_id"] for i in group}),
                "items": group,
                "delivered_at": None,
                "reminder_cycle": 0,
            },
        )
        if carry and key in carry and carry[key].status in {"acknowledged", "inherited"}:
            source = carry[key]
            ob.status = "inherited"
            ob.data = {
                **ob.data,
                "inherited_from": source.id,
                "reason": "Автор явно отменил необходимость повторного ознакомления",
            }
        db.add(ob)
        db.flush()
        if r.data["mode"] == "task":
            create_task(db, p, r, ob, item, sender)
    for binding_id in sorted({i["binding_id"] for i in items}):
        old = db.scalar(
            select(PacketPublication).where(
                PacketPublication.revision_id == r.id, PacketPublication.binding_id == binding_id
            )
        )
        if old:
            continue
        group = [i for i in items if i["binding_id"] == binding_id]
        pub = PacketPublication(
            revision_id=r.id,
            binding_id=binding_id,
            binding_version=group[0]["binding_version"],
            data={"items": group, "outbox_ids": []},
            version=1,
        )
        db.add(pub)
        db.flush()
        enqueue_publication(db, p, r, pub, sender)


def create_task(db, p, r, ob, item, sender):
    base = support_route(db, {**item, "brand_id": ob.brand_id})
    w = Work(
        kind="packet_task",
        title=(p.title + " · " + item["name"])[:250],
        partner_id=ob.partner_id,
        establishment_id=ob.establishment_id,
        brand_id=ob.brand_id,
        owner_id=ob.person_id,
        executor_id=ob.person_id,
        reviewer_id=ob.curator_id,
        partner_person_id=ob.person_id,
        binding_id=base.id,
        review_binding_id=base.id,
        client_binding_id=ob.binding_id,
        due_at=ob.due_at,
        original_due_at=ob.due_at,
        origin_key="packet-task:" + ob.id,
        data={
            "packet_obligation_id": ob.id,
            "packet_revision_id": r.id,
            "checklist": r.data["checklist"],
            "evidence_requirements": r.data.get("evidence_requirements", "Результат по чек-листу"),
            "calendar": r.data.get("calendar", "support"),
        },
    )
    db.add(w)
    db.flush()
    ob.work_id = w.id
    # The obligation owns client reminders; ordinary acceptance still owns the separate review SLA.
    from ama.sla import initialize, invalidate

    c = initialize(db, w, explicit_due=True)
    invalidate(db, c, w, "take", "result", "before", "day")
    topic(db, p, base, "work", w.id, w.title, sender=sender)


def packet_text(db, p, r, pub):
    current = revision(db, p)
    archived = r.number != p.current_revision and parse_date(current.data["apply_at"]) <= now()
    body = r.data["text"]
    if archived:
        import re

        body = re.sub(r"https://\S+", "[ссылка прежней редакции]", body)
    lines = [
        r.data["title"],
        f"Редакция {r.number}",
        body,
        "Дата применения: " + r.data["apply_at"],
        "Предприятия: " + ", ".join(sorted({i["name"] for i in pub.data["items"]})),
    ]
    if r.data["mode"] != "information":
        lines += [
            "Срок: " + r.data["due_at"],
            "Режим: " + ("Ознакомление" if r.data["mode"] == "acknowledgement" else "Задание с результатом"),
        ]
    for row in r.data.get("checklist", []):
        lines.append("• " + row)
    for item in r.audience["materials"]:
        live = db.get(Record, item["id"])
        active = not archived and live and live.published and live.version == item["version"]
        lines.append(
            item["title"] + (": " + item["url"] if active else " — прежняя редакция; используйте актуальный пакет")
        )
    for item in r.data.get("links", []):
        lines.append(item["title"] + (": " + item["url"] if not archived else " — ссылка прежней редакции"))
    return "\n".join(lines)


def enqueue_publication(db, p, r, pub, sender=None):
    sender = sender or Actor(p.author_id, r.data.get("author_binding", p.source_binding_id))
    signature = {
        "person_id": sender.person_id,
        "binding_id": sender.binding_id,
        "grants": access.grants_hash(db, sender),
    }
    binding = db.get(Binding, pub.binding_id)
    parts = []
    chunks = message_parts(packet_text(db, p, r, pub))
    for index, body in enumerate(chunks):
        row = queue(
            db,
            binding,
            f"packet:{pub.id}:text:{index}:v{pub.version}",
            body,
            buttons=[["Открыть пакет", "af:pack:" + r.id]] if index == len(chunks) - 1 else [],
        )
        parts.append(row)
    for asset in r.audience["assets"]:
        row = queue(
            db, binding, f"packet:{pub.id}:asset:{asset['id']}:v{pub.version}", p.title + " · " + asset["filename"]
        )
        row.payload = {**row.payload, "packet_asset": asset["id"], "filename": asset["filename"]}
        parts.append(row)
    for row in parts:
        row.payload = {
            **row.payload,
            "packet_publication": pub.id,
            "packet_revision": r.id,
            "partner_window": True,
            "packet_sender": signature,
        }
    pub.data = {**pub.data, "outbox_ids": pub.data["outbox_ids"] + [row.id for row in parts if row.id]}
    db.flush()
    # Flush generated ids before persisting the delivery manifest.
    pub.data = {**pub.data, "outbox_ids": list(dict.fromkeys(pub.data["outbox_ids"] + [row.id for row in parts]))}


def topic(db, p, base, target, target_id, title, sender=None):
    if base.platform == "telegram":
        row = queue(db, base, f"packet-topic:{target}:{target_id}", title[:100])
        row.payload = {
            "packet_sender": {
                "person_id": sender.person_id,
                "binding_id": sender.binding_id,
                "grants": access.grants_hash(db, sender),
            }
            if sender
            else None,
            "op": "create_topic",
            "topic_name": title[:100],
            "packet_target": target,
            "target_id": target_id,
            "packet_id": p.id,
        }
    elif target == "packet":
        p.topic_binding_id = base.id
        queue(
            db,
            base,
            "packet-author:" + p.id,
            "Пакет: " + p.title + "\n/packets — сводка",
            buttons=[["Рассылки ФП", "af:packets"]],
        )


def announce(db, p):
    topic(db, p, db.get(Binding, p.source_binding_id), "packet", p.id, "Рассылка · " + p.title)


def on_topic(db, row, external_id):
    p = get(db, row.payload["packet_id"], True)
    base = db.get(Binding, row.binding_id)
    if not base or not base.active or not base.verified or base.version != row.binding_version or not external_id:
        raise BusinessError("Созданная тема требует сверки актуального маршрута.")
    target = row.payload["packet_target"]
    w = db.get(Work, row.payload["target_id"]) if target == "work" else None
    topic_id = str(int(external_id))
    b = db.scalar(
        select(Binding).where(
            Binding.bot_id == p.bot_id,
            Binding.platform == base.platform,
            Binding.chat_id == base.chat_id,
            Binding.topic_key == topic_id,
        )
    )
    if not b:
        b = Binding(
            bot_id=p.bot_id,
            platform=base.platform,
            chat_id=base.chat_id,
            topic_key=topic_id,
            purpose=base.purpose,
            partner_id=w.partner_id if w else None,
            brand_id=w.brand_id if w else None,
            establishment_id=w.establishment_id if w else None,
            active=True,
            verified=True,
        )
        db.add(b)
        db.flush()
    if w:
        w.binding_id = w.review_binding_id = b.id
        queue(
            db, b, "packet-work-topic:" + w.id, w.title + "\nID: " + w.id, work=w, buttons=[["Открыть", "af:w:" + w.id]]
        )
    else:
        p.topic_binding_id = b.id
        refresh_summary(db, p, "initial")
        queue(
            db,
            b,
            "packet-author:" + p.id,
            "Пакет: " + p.title + "\n/packets — сводка",
            buttons=[["Рассылки ФП", "af:packets"]],
        )


def summary(db, p):
    r = revision(db, p)
    obs = list(db.scalars(select(PacketObligation).where(PacketObligation.revision_id == r.id)))
    pubs = list(db.scalars(select(PacketPublication).where(PacketPublication.revision_id == r.id)))
    return {
        "id": p.id,
        "version": p.version,
        "revision_id": r.id,
        "revision": r.number,
        "title": p.title,
        "chats": len(pubs),
        "partners": len({i["partner_id"] for i in r.audience["included"]}),
        "establishments": len({i["establishment_id"] for i in r.audience["included"]}),
        "obligations": len(obs),
        "accepted": sum(status(db, o) in {"acknowledged", "inherited", "accepted"} for o in obs),
        "sent": sum(publication_state(db, x) == "sent" for x in pubs),
    }


def publication_state(db, pub):
    rows = [db.get(Outbox, ident) for ident in pub.data.get("outbox_ids", [])]
    if rows and all(row and row.status == "sent" for row in rows):
        return "sent"
    if any(row and row.status in {"failed", "blocked", "uncertain", "sending", "cancelled"} for row in rows):
        return "error"
    return "queued"


def status(db, ob):
    if ob.status in {"acknowledged", "inherited", "cancelled"}:
        return ob.status
    if ob.work_id:
        w = db.get(Work, ob.work_id)
        if w and w.status == "done":
            return "accepted" if w.outcome == "accepted" else "cancelled"
        if w and w.status in {"review", "rework"}:
            return w.status
    if ob.status == "delivery_issue":
        return "delivery_issue"
    if ob.data.get("delivered_at") and ob.due_at < now():
        return "overdue"
    return "pending"


def visible_items(db, actor, r):
    b = db.get(Binding, actor.binding_id)
    if not b or not b.active or not b.verified or b.bot_id != settings().bot_id or b.purpose != "client":
        return []
    pub = db.scalar(
        select(PacketPublication).where(PacketPublication.revision_id == r.id, PacketPublication.binding_id == b.id)
    )
    if not pub:
        return []
    return [
        i
        for i in pub.data["items"]
        if i["partner_id"] == b.partner_id
        and i["brand_id"] == b.brand_id
        and active_person_role(
            db,
            actor.person_id,
            {"partner", "observer"},
            SimpleNamespace(partner_id=i["partner_id"], brand_id=i["brand_id"], establishment_id=i["establishment_id"]),
        )
        and db.scalar(
            select(EstablishmentBrand.id).where(
                EstablishmentBrand.establishment_id == i["establishment_id"],
                EstablishmentBrand.brand_id == i["brand_id"],
                EstablishmentBrand.active.is_(True),
            )
        )
    ]


def ob_visible(db, actor, ob, *, acting=False):
    r = db.get(PacketRevision, ob.revision_id)
    items = visible_items(db, actor, r)
    matching = [i for i in items if scope_key(r.data, i) == ob.scope_key]
    if not matching:
        return False
    b = db.get(Binding, actor.binding_id)
    scope = SimpleNamespace(partner_id=ob.partner_id, brand_id=b.brand_id, establishment_id=ob.establishment_id)
    if acting:
        return actor.person_id == ob.person_id and active_person_role(db, actor.person_id, {"partner"}, scope)
    return True


def staff_visible(db, actor, p, r=None):
    r = r or revision(db, p)
    return any(
        access.scope(
            db,
            actor,
            SimpleNamespace(**{k: i.get(k) for k in ("partner_id", "brand_id", "establishment_id")}),
            {"curator", "support_head", "marketing_manager"},
        )
        for i in r.audience["included"]
    )


def authorize_ob(db, actor, ob, acting=False):
    p = get(db, db.get(PacketRevision, ob.revision_id).packet_id)
    if ob_visible(db, actor, ob, acting=acting):
        return p
    if not acting and staff_visible(db, actor, p):
        return p
    raise AccessDenied("Обязательство недоступно этому пользователю в этом чате.")


def work_presentation(db, actor, w):
    """Only registered packet presentations may expose a shared physical Work in another brand."""
    b = db.get(Binding, actor.binding_id)
    if not b or b.purpose != "client" or w.kind != "packet_task":
        return False
    for ob in db.scalars(select(PacketObligation).where(PacketObligation.work_id == w.id)):
        if ob_visible(db, actor, ob):
            return True
    return False


def acknowledge(db, actor, ob_id, expected, data):
    source = db.get(PacketObligation, ob_id)
    if not source:
        raise BusinessError("Обязательство не найдено.")
    p = get(db, db.get(PacketRevision, source.revision_id).packet_id, True)
    ob = db.scalar(
        select(PacketObligation)
        .where(PacketObligation.id == ob_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    authorize_ob(db, actor, ob, True)
    if ob.status in {"acknowledged", "inherited"}:
        return {"id": ob.id, "version": ob.version, "status": ob.status}
    versioned(ob, expected)
    r = db.get(PacketRevision, ob.revision_id)
    if r.data["mode"] != "acknowledgement" or ob.status == "cancelled" or p.current_revision != r.number:
        raise BusinessError("Эта редакция не ожидает ознакомления.")
    pubs = list(
        db.scalars(
            select(PacketPublication).where(
                PacketPublication.revision_id == r.id, PacketPublication.binding_id == actor.binding_id
            )
        )
    )
    if not pubs or not pubs[0].delivered_at:
        raise BusinessError("Пакет ещё не доставлен полностью; ознакомление не подтверждено.")
    ob.status = "acknowledged"
    ob.version += 1
    ob.data = {
        **ob.data,
        "acknowledged_by": actor.person_id,
        "acknowledged_at": now().isoformat(),
        "source_binding": actor.binding_id,
    }
    refresh_summary(db, p, "ack:" + ob.id + ":" + str(ob.version))
    stamp(db, actor, ob, "PACKET_ACKNOWLEDGED", data={"packet_id": p.id, "revision_id": r.id})
    return {
        "id": ob.id,
        "version": ob.version,
        "status": ob.status,
        "message": "Подтверждение единое; во всех представлениях показывается актуальная отметка.",
    }


def submit(db, actor, ob_id, expected, data, key):
    ob = db.get(PacketObligation, ob_id)
    if not ob:
        raise BusinessError("Обязательство не найдено.")
    authorize_ob(db, actor, ob, True)
    versioned(ob, expected)
    r = db.get(PacketRevision, ob.revision_id)
    p = get(db, r.packet_id, True)
    if not ob.work_id or ob.status == "cancelled" or p.current_revision != r.number:
        raise BusinessError("Нет действующего задания этой редакции.")
    if not ob.data.get("delivered_at"):
        raise BusinessError("Пакет ещё не доставлен полностью.")
    if ob.status == "delivery_issue":
        raise BusinessError("Публикация доставлена после срока. Куратор должен сначала назначить будущий срок явно.")
    if set(data.get("completed_items", [])) != set(r.data["checklist"]) or not data.get("result_description"):
        raise BusinessError("Подтвердите каждый применимый пункт и опишите результат.")
    evidence = r.data.get("evidence_mode", "text")
    if evidence in {"file", "photo"}:
        files = list(
            db.scalars(
                select(Attachment).where(
                    Attachment.work_id == ob.work_id,
                    Attachment.author_id == actor.person_id,
                    Attachment.visibility == "client",
                    Attachment.status == "stored",
                )
            )
        )
        if evidence == "photo":
            files = [a for a in files if Path(a.filename).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]
        if not files:
            raise BusinessError("Сначала сохраните требуемое доказательство в этом пункте.")
    from ama.service import transition

    w = db.get(Work, ob.work_id)
    expected_work = data.get("work_version")
    versioned(w, expected_work)
    if w.status == "new":
        transition(db, actor, w.id, w.version, "take", {}, key + ":take")
    proof = db.info.setdefault("packet_submit", set())
    proof.add(w.id)
    try:
        transition(
            db, actor, w.id, w.version, "submit", {"result_description": data["result_description"]}, key + ":submit"
        )
    finally:
        proof.discard(w.id)
    ob.version += 1
    ob.data = {
        **ob.data,
        "completed_items": data["completed_items"],
        "submitted_by": actor.person_id,
        "submitted_at": now().isoformat(),
    }
    return {"id": ob.id, "version": ob.version, "work_id": w.id, "status": status(db, ob)}


def before_transition(db, actor, w, action, data):
    if w.kind != "packet_task":
        return
    ob = db.scalar(select(PacketObligation).where(PacketObligation.work_id == w.id))
    if not ob:
        raise BusinessError("Задание не связано с обязательством пакета.")
    if action == "submit":
        # submit() installs a transaction-local proof; arbitrary /act JSON cannot forge it.
        proof = db.info.get("packet_submit", set())
        if w.id not in proof:
            raise BusinessError("Сдайте результат через чек-лист карточки пакета.")
    if action in {"accept", "reject"}:
        if w.reviewer_id != ob.curator_id and active_person_role(db, w.reviewer_id, {"curator", "support_head"}, w):
            ob.curator_id = w.reviewer_id
            ob.version += 1
            stamp(db, actor, ob, "PACKET_CURATOR_SUBSTITUTION", data={"reviewer_id": w.reviewer_id})
        if actor.person_id != ob.curator_id or not access.scope(db, actor, w, {"curator", "support_head"}):
            raise AccessDenied("Объектное исполнение принимает закреплённый куратор.")
    if action == "cancel" and db.get(Binding, actor.binding_id).purpose == "client":
        raise AccessDenied("ФП не отменяет задание самостоятельно.")


def question(db, actor, revision_id, expected, data, key):
    r = db.get(PacketRevision, revision_id)
    if not r:
        raise BusinessError("Редакция не найдена.")
    p = get(db, r.packet_id, True)
    versioned(r, expected)
    items = visible_items(db, actor, r)
    selected = data.get("establishment_id")
    if selected:
        items = [i for i in items if i["establishment_id"] == selected]
    if not items or not data.get("question"):
        raise AccessDenied("Выберите своё предприятие и укажите вопрос.")
    item = items[0]
    scope = SimpleNamespace(partner_id=item["partner_id"], brand_id=item["brand_id"], establishment_id=selected)
    if not active_person_role(db, actor.person_id, {"partner"}, scope) or actor.person_id != item["person_id"]:
        raise AccessDenied("Вопрос создаёт назначенный ФП.")
    key_scope = selected or "partner"
    q = db.scalar(
        select(PacketQuestion).where(
            PacketQuestion.revision_id == r.id,
            PacketQuestion.partner_id == item["partner_id"],
            PacketQuestion.person_id == actor.person_id,
            PacketQuestion.scope_key == key_scope,
        )
    )
    if q:
        w = db.get(Work, q.work_id)
    else:
        base = support_route(db, item)
        due = Calendar(r.audience["calendar"]).end_of_workday(now(), 2) if r.audience["calendar"] else None
        if not due:
            calendar = db.scalar(
                select(Record).where(Record.kind == "calendar", Record.key == "support", Record.published.is_(True))
            )
            if not calendar:
                raise BusinessError("Для вопроса нужен опубликованный календарь сопровождения.")
            due = Calendar(calendar.data).end_of_workday(now(), 2)
        w = Work(
            kind="packet_question",
            title=("Вопрос по пакету: " + p.title)[:250],
            partner_id=item["partner_id"],
            brand_id=item["brand_id"],
            establishment_id=selected,
            owner_id=item["curator_id"],
            executor_id=item["curator_id"],
            reviewer_id=item["curator_id"],
            partner_person_id=actor.person_id,
            binding_id=base.id,
            review_binding_id=base.id,
            client_binding_id=actor.binding_id,
            due_at=due,
            original_due_at=due,
            origin_key="packet-question:" + key,
            data={"packet_id": p.id, "packet_revision_id": r.id, "calendar": "support"},
        )
        db.add(w)
        db.flush()
        q = PacketQuestion(
            revision_id=r.id,
            partner_id=item["partner_id"],
            person_id=actor.person_id,
            scope_key=key_scope,
            work_id=w.id,
        )
        db.add(q)
        from ama.sla import initialize

        initialize(db, w, explicit_due=False)
        topic(db, p, base, "work", w.id, w.title)
        message = db.scalar(select(Outbox).where(Outbox.dedupe_key == f"packet-topic:work:{w.id}"))
        if message:
            message.payload = {**message.payload, "packet_question_topic": True}
    if w.status == "done":
        w.status = "new"
        w.outcome = None
        w.owner_id = w.executor_id
        w.version += 1
        from ama.sla import case_for, planned, clock

        c = case_for(db, w, True)
        if c:
            w.due_at = clock(c).end_of_workday(now(), 2)
            c.data = {**c.data, "closed_at": None}
            planned(db, c, w, w.due_at)
    w.data = {
        **w.data,
        "public_questions": w.data.get("public_questions", [])
        + [{"text": data["question"], "by": actor.person_id, "at": now().isoformat()}],
    }
    queue(
        db,
        db.get(Binding, w.binding_id),
        "packet-question-message:" + key,
        "Вопрос ФП по пакету " + p.title + ":\n" + data["question"],
        work=w,
        buttons=[["Открыть", "af:w:" + w.id]],
    )
    stamp(db, actor, w, "PACKET_QUESTION", data={"revision_id": r.id, "question": data["question"]})
    return {"id": w.id, "version": w.version}


def delivered(db, row):
    if row.payload.get("packet_answer") and row.status == "sent":
        answer_delivered(db, row)
        return
    if row.payload.get("packet_reminder") and row.status == "sent":
        ids = row.payload.get("packet_reminder_ids") or [row.payload["packet_reminder"]]
        for ob in db.scalars(
            select(PacketObligation)
            .where(PacketObligation.id.in_(ids))
            .order_by(PacketObligation.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ):
            if ob.data.get("reminder_outbox") == row.id:
                ob.data = {
                    **ob.data,
                    "last_reminder": now().isoformat(),
                    "reminder_outbox": None,
                    "reminder_cycle": ob.data.get("reminder_cycle", 0) + 1,
                }
        return
    ident = row.payload.get("packet_publication")
    if not ident or row.status != "sent":
        return
    pub = db.scalar(
        select(PacketPublication)
        .where(PacketPublication.id == ident)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not pub or pub.delivered_at or publication_state(db, pub) != "sent":
        return
    pub.delivered_at = now()
    pub.version += 1
    r = db.get(PacketRevision, pub.revision_id)
    for ob in db.scalars(
        select(PacketObligation)
        .where(PacketObligation.revision_id == r.id)
        .order_by(PacketObligation.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ):
        if any(scope_key(r.data, i) == ob.scope_key for i in pub.data["items"]) and not ob.data.get("delivered_at"):
            ob.binding_id = pub.binding_id
            ob.data = {**ob.data, "delivered_at": now().isoformat(), "delivered_publication": pub.id}
            if ob.due_at <= now() and ob.status == "pending":
                ob.status = "delivery_issue"
                ob.data = {**ob.data, "delivery_late": True}
                try:
                    base = support_route(
                        db,
                        {"partner_id": ob.partner_id, "brand_id": ob.brand_id, "establishment_id": ob.establishment_id},
                    )
                    queue(
                        db,
                        base,
                        "packet-late-delivery:" + ob.id,
                        "Пакет доставлен после исходного срока. Это проблема доставки, не бездействие ФП. Назначьте будущий срок явно.\nОбязательство: "
                        + ob.id,
                    )
                except BusinessError as error:
                    ob.data = {**ob.data, "control_error": str(error)}
    refresh_summary(db, get(db, r.packet_id), "delivery:" + pub.id + ":" + str(pub.version))
    stamp(db, None, pub, "PACKET_DELIVERED", data={"revision_id": r.id})


def deliverable(db, row):
    if row.payload.get("packet_answer"):
        w = db.get(Work, row.payload["packet_answer"])
        actor = Actor(row.payload["packet_answer_actor"], row.payload["packet_answer_binding"])
        if (
            not w
            or w.owner_id != actor.person_id
            or w.client_binding_id != row.binding_id
            or access.grants_hash(db, actor) != row.payload["packet_answer_grants"]
        ):
            raise BusinessError("Ответственный или права изменились до отправки ответа.")
        from ama.auth import require

        require(db, actor, STAFF, w)
        return True
    if row.payload.get("packet_question_topic"):
        w = db.get(Work, row.payload["target_id"])
        if not w or w.kind != "packet_question" or not active_person_role(db, w.owner_id, STAFF, w):
            raise BusinessError("Не назначен действующий ответственный по вопросу.")
        return True
    if row.payload.get("packet_reminder"):
        open_obs = reminder_open(db, row)
        if not open_obs:
            return False
        r = db.get(PacketRevision, open_obs[0].revision_id)
        row.payload = {
            **row.payload,
            "text": reminder_text(db, r, open_obs),
            "packet_reminder_ids": [o.id for o in open_obs],
        }
        return True
    ident = row.payload.get("packet_publication")
    if not ident and not row.payload.get("packet_target"):
        return True
    r = db.get(PacketRevision, row.payload.get("packet_revision")) if ident else None
    p = get(db, r.packet_id if r else row.payload["packet_id"])
    signer = row.payload.get("packet_sender")
    actor = (
        Actor(signer["person_id"], signer["binding_id"])
        if signer
        else Actor(p.author_id, r.data.get("author_binding", p.source_binding_id) if r else p.source_binding_id)
    )
    if r:
        if p.current_revision != r.number:
            return False
        if access.grants_hash(db, actor) != (signer["grants"] if signer else r.audience["author_grants"]):
            raise BusinessError("Права автора изменились после подтверждения рассылки.")
        pub = db.get(PacketPublication, ident)
        if not pub or pub.binding_id != row.binding_id:
            return False
        if row.id not in pub.data.get("outbox_ids", []):
            return False
        for item in pub.data["items"]:
            publisher(
                db,
                actor,
                SimpleNamespace(**{k: item.get(k) for k in ("partner_id", "brand_id", "establishment_id")}),
                r.data["audience"] == "general",
            )
            link = db.scalar(
                select(EstablishmentBrand).where(
                    EstablishmentBrand.establishment_id == item["establishment_id"],
                    EstablishmentBrand.brand_id == item["brand_id"],
                    EstablishmentBrand.active.is_(True),
                )
            )
            if not active_person_role(
                db,
                item["person_id"],
                {"partner"},
                SimpleNamespace(
                    partner_id=item["partner_id"], brand_id=item["brand_id"], establishment_id=item["establishment_id"]
                ),
            ) or not db.scalar(
                select(Account.id).where(
                    Account.person_id == item["person_id"], Account.platform == row.platform, Account.active.is_(True)
                )
            ):
                raise BusinessError("Назначение или аккаунт получателя изменились; проверьте подключение.")
            if not link:
                raise BusinessError("Матрица подключений изменилась; нужна проверка публикации.")
        for material in r.audience["materials"]:
            live = db.get(Record, material["id"])
            if (
                not live
                or not live.published
                or live.version != material["version"]
                or "partner" not in live.data.get("audience", [])
            ):
                raise BusinessError("Материал изменился перед отправкой; подтвердите новую редакцию.")
    else:
        publisher(db, actor)
    return True


def asset_path(db, row):
    ident = row.payload.get("packet_asset")
    a = db.get(PacketAsset, ident)
    r = db.get(PacketRevision, row.payload.get("packet_revision"))
    expected = next((x for x in r.audience["assets"] if x["id"] == ident), None) if r else None
    if not a or a.status != "stored" or not expected or a.sha256 != expected["sha256"]:
        raise BusinessError("Файл пакета не совпадает с подтверждённым предпросмотром.")
    path = Path(a.storage_path).resolve()
    if (
        Path(settings().file_root).resolve() not in path.parents
        or not path.is_file()
        or sha256(path.read_bytes()).hexdigest() != a.sha256
    ):
        raise BusinessError("Сохранённый файл не прошёл проверку целостности.")
    return path


def asset_receive(db, actor, event):
    from ama.models import UIScreen

    s = db.scalar(
        select(UIScreen).where(
            UIScreen.bot_id == settings().bot_id,
            UIScreen.person_id == actor.person_id,
            UIScreen.binding_id == actor.binding_id,
        )
    )
    prompt = s.state.get("prompt") if s else None
    if not prompt or prompt.get("type") != "packet_asset":
        return False
    if datetime.fromisoformat(prompt["expires"]) < now():
        raise BusinessError("Контекст загрузки истёк; откройте пакет заново.")
    from ama.ui.engine import view_draft, update

    draft = view_draft(db, actor, prompt["draft_id"])
    publisher(db, actor)
    target = draft.data.get("payload", {}) if draft.kind in {"packet:revise", "packet:schedule"} else draft.data
    ids = list(target.get("asset_ids", []))
    for i, f in enumerate(event.files):
        if Path(f["filename"]).suffix.lower() not in {
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
            ".pdf",
            ".doc",
            ".docx",
            ".xls",
            ".xlsx",
            ".zip",
            ".txt",
        }:
            raise BusinessError("Видео добавляется внешней ссылкой; тип этого файла не поддерживается.")
        key = event.key + ":packet:" + str(i)
        a = db.scalar(select(PacketAsset).where(PacketAsset.source_key == key))
        if not a:
            a = PacketAsset(
                author_id=actor.person_id,
                binding_id=actor.binding_id,
                draft_id=draft.id,
                platform=event.platform,
                file_id=f["file_id"],
                filename=f["filename"][:250],
                source_key=key,
            )
            db.add(a)
            db.flush()
        if a.id not in ids:
            ids.append(a.id)
    update(
        draft,
        {**draft.data, "payload": {**target, "asset_ids": ids}}
        if draft.kind in {"packet:revise", "packet:schedule"}
        else {**draft.data, "asset_ids": ids},
    )
    return True


def asset_step(db, transport):
    a = db.scalar(select(PacketAsset).where(PacketAsset.status == "pending").limit(1).with_for_update(skip_locked=True))
    if not a:
        return False
    from ama.files import download

    try:
        a.storage_path, a.size, a.sha256 = download(a, transport.client)
        a.status = "stored"
    except Exception as error:
        a.status = "failed"
        stamp(db, None, a, "PACKET_ASSET_FAILED", data={"error": type(error).__name__})
    return True


def authorize_edit(db, actor, p, r=None):
    r = r or revision(db, p)
    if p.author_id != actor.person_id:
        raise AccessDenied("Изменяет пакет его автор; право рассылки не меняет куратора.")
    for item in r.audience["included"]:
        publisher(
            db,
            actor,
            SimpleNamespace(**{k: item.get(k) for k in ("partner_id", "brand_id", "establishment_id")}),
            r.data["audience"] == "general",
        )


def revise(db, actor, packet_id, expected, data, key):
    p = get(db, packet_id, True)
    versioned(p, expected)
    old = revision(db, p)
    authorize_edit(db, actor, p, old)
    if (
        not data.get("reason")
        or not isinstance(data.get("repeat_required"), bool)
        or not isinstance(data.get("cancel_pending"), bool)
    ):
        raise BusinessError(
            "Опишите существенное изменение и явно решите, нужны ли повторное ознакомление и отмена старых обязательств."
        )
    payload = data["payload"]
    snap = audience(db, actor, payload)
    obs = list(db.scalars(select(PacketObligation).where(PacketObligation.revision_id == old.id)))
    if old.data["mode"] == "task" and not data["repeat_required"]:
        raise BusinessError("Новые условия объектного задания требуют отдельного результата новой редакции.")
    pending = [o for o in obs if status(db, o) not in {"acknowledged", "inherited", "accepted", "cancelled"}]
    if pending and not data["cancel_pending"]:
        raise BusinessError(
            "Старые обязательства не исчезают. Согласуйте их явную отмену с причиной перед новой редакцией."
        )
    for ob in pending:
        if ob.work_id:
            w = db.get(Work, ob.work_id)
            if (
                w.status not in {"new", "waiting_partner"}
                and actor.person_id != ob.curator_id
                and not access.scope(db, actor, w, {"support_head"})
            ):
                raise AccessDenied("Изменение уже исполняемого задания сначала согласует закреплённый куратор.")
            if w.data.get("paid_started_at") or w.data.get("procurement_ordered"):
                raise BusinessError(
                    "Заказанная/оплаченная работа требует отдельного согласования; редакция не отменяет её автоматически."
                )
            w.status, w.outcome = "done", "cancelled"
            w.version += 1
            w.data = {**w.data, "packet_cancellation_reason": data["reason"]}
            stamp(db, actor, w, "PACKET_OLD_WORK_CANCELLED", data={"reason": data["reason"]})
        ob.status = "cancelled"
        ob.version += 1
        ob.data = {**ob.data, "cancellation_reason": data["reason"]}
    p.current_revision += 1
    p.version += 1
    p.title = payload["title"]
    r = PacketRevision(
        id=key,
        packet_id=p.id,
        number=p.current_revision,
        data={
            **deepcopy(payload),
            "author_binding": actor.binding_id,
            "change_reason": data["reason"],
            "repeat_required": data["repeat_required"],
        },
        audience=snap,
    )
    db.add(r)
    db.flush()
    carry = {o.scope_key: o for o in obs} if not data["repeat_required"] else None
    make_recipients(db, p, r, snap["included"], carry=carry, sender=actor)
    stamp(
        db,
        actor,
        p,
        "PACKET_REVISED",
        data={
            "previous_revision": old.id,
            "revision": r.id,
            "reason": data["reason"],
            "repeat_required": data["repeat_required"],
        },
    )
    return {**summary(db, p), "fingerprint": snap["fingerprint"], "audience": snap}


def typo(db, actor, packet_id, expected, data):
    p = get(db, packet_id, True)
    versioned(p, expected)
    r = revision(db, p)
    authorize_edit(db, actor, p, r)
    if not data.get("text") or len(data["text"]) > 2500 or not data.get("reason"):
        raise BusinessError(
            "Для опечатки нужны исправленный текст и причина. Срок, материалы и условия меняются новой редакцией."
        )
    history = list(r.data.get("typo_history", [])) + [
        {"text": r.data["text"], "by": actor.person_id, "at": now().isoformat(), "reason": data["reason"]}
    ]
    r.data = {**r.data, "text": data["text"], "typo_history": history}
    r.version += 1
    p.version += 1
    for pub in db.scalars(select(PacketPublication).where(PacketPublication.revision_id == r.id)):
        retained = []
        retired = []
        for ident in pub.data["outbox_ids"]:
            row = db.get(Outbox, ident)
            if row and row.payload.get("packet_asset"):
                retained.append(ident)
            elif row:
                retired.append({"id": row.id, "status": row.status})
                if row.status in {"pending", "failed", "blocked"}:
                    row.status = "cancelled"
        pub.version += 1
        body = "Исправлена опечатка (без нового обязательства):\n" + packet_text(db, p, r, pub)
        parts = []
        for i, chunk in enumerate(message_parts(body)):
            row = queue(
                db,
                db.get(Binding, pub.binding_id),
                f"packet-typo:{p.id}:{p.version}:{pub.id}:{i}",
                chunk,
                buttons=[["Открыть пакет", "af:pack:" + r.id]] if i == len(message_parts(body)) - 1 else [],
            )
            row.payload = {
                **row.payload,
                "packet_revision": r.id,
                "packet_publication": pub.id,
                "partner_window": True,
                "packet_sender": {
                    "person_id": actor.person_id,
                    "binding_id": actor.binding_id,
                    "grants": access.grants_hash(db, actor),
                },
            }
            parts.append(row)
        db.flush()
        pub.data = {
            **pub.data,
            "outbox_ids": retained + [row.id for row in parts],
            "retired_parts": pub.data.get("retired_parts", []) + retired,
            "delivery_history": pub.data.get("delivery_history", [])
            + ([pub.delivered_at.isoformat()] if pub.delivered_at else []),
        }
        pub.delivered_at = None
    stamp(db, actor, p, "PACKET_TYPO_CORRECTED", data={"reason": data["reason"], "revision_id": r.id})
    return summary(db, p)


def add_recipients(db, actor, packet_id, expected, data):
    p = get(db, packet_id, True)
    versioned(p, expected)
    r = revision(db, p)
    publisher(db, actor)
    if (
        actor.person_id != p.author_id
        and not access.staff_role(db, actor, "curator")
        and not access.staff_role(db, actor, "support_head")
    ):
        raise AccessDenied("Новых получателей добавляет автор или уполномоченный куратор.")
    if not data.get("establishment_ids") or not data.get("reason"):
        raise BusinessError("Выберите новые предприятия и основание включения в действующий пакет.")
    if r.data["mode"] != "information" and parse_date(r.data["due_at"]) <= now():
        raise BusinessError(
            "Срок пакета прошёл. Создайте новую редакцию с будущим сроком, не назначайте его задним числом."
        )
    payload = {**r.data, "establishment_ids": data["establishment_ids"]}
    snap = audience(db, actor, payload, r.audience["assets"])
    if snap["materials"] != r.audience["materials"] or snap["assets"] != r.audience["assets"]:
        raise BusinessError("Материалы изменились; сначала требуется новая редакция пакета.")
    old_keys = {(i["establishment_id"], i["brand_id"]) for i in r.audience["included"]}
    fresh = [i for i in snap["included"] if (i["establishment_id"], i["brand_id"]) not in old_keys]
    for item in fresh:
        previous = db.scalar(
            select(PacketObligation).where(
                PacketObligation.revision_id == r.id, PacketObligation.scope_key == scope_key(r.data, item)
            )
        )
        if previous and (
            previous.person_id != item["person_id"]
            or r.data["mode"] == "task"
            and previous.curator_id != item["curator_id"]
        ):
            raise BusinessError(
                "Общее обязательство уже имеет назначенного ФП/куратора. Сначала согласуйте назначения, не создавая дубль."
            )
    if not fresh:
        raise BusinessError("Новых применимых подключений нет.")
    r.audience = {
        **r.audience,
        "included": r.audience["included"] + fresh,
        "additions": r.audience.get("additions", [])
        + [{"at": now().isoformat(), "by": actor.person_id, "reason": data["reason"], "items": fresh}],
    }
    # Preserve existing publications and proofs. Only new obligations and supplemental delivery are added.
    old_pub = {
        pub.binding_id: pub
        for pub in db.scalars(select(PacketPublication).where(PacketPublication.revision_id == r.id))
    }
    make_recipients(db, p, r, fresh, sender=actor)
    for binding_id, pub in old_pub.items():
        group = [i for i in fresh if i["binding_id"] == binding_id]
        if group:
            pub.data = {
                **pub.data,
                "items": pub.data["items"] + group,
                "delivery_history": pub.data.get("delivery_history", [])
                + ([pub.delivered_at.isoformat()] if pub.delivered_at else []),
            }
            pub.delivered_at = None
            pub.version += 1
            row = queue(
                db,
                db.get(Binding, binding_id),
                f"packet-add:{pub.id}:v{pub.version}",
                "В действующий пакет добавлены предприятия: " + ", ".join(i["name"] for i in group) + "\n" + p.title,
                buttons=[["Открыть пакет", "af:pack:" + r.id]],
            )
            row.payload = {
                **row.payload,
                "packet_revision": r.id,
                "packet_publication": pub.id,
                "partner_window": True,
                "packet_sender": {
                    "person_id": actor.person_id,
                    "binding_id": actor.binding_id,
                    "grants": access.grants_hash(db, actor),
                },
            }
            db.flush()
            pub.data = {**pub.data, "outbox_ids": pub.data["outbox_ids"] + [row.id]}
    r.version += 1
    p.version += 1
    stamp(db, actor, p, "PACKET_RECIPIENTS_ADDED", data={"reason": data["reason"], "items": fresh})
    return {**summary(db, p), "fingerprint": snap["fingerprint"]}


def replace_binding(db, old, new):
    for pub in db.scalars(select(PacketPublication).where(PacketPublication.binding_id == old.id).with_for_update()):
        # A replacement describes the same client scope. Do not send a second copy of delivered parts.
        pub.data = {
            **pub.data,
            "route_history": pub.data.get("route_history", []) + [{"old_binding": old.id, "new_binding": new.id}],
            "items": [{**i, "binding_id": new.id, "binding_version": new.version} for i in pub.data["items"]],
        }
        pub.binding_id = new.id
        pub.binding_version = new.version
        pub.version += 1
    for ob in db.scalars(select(PacketObligation).where(PacketObligation.binding_id == old.id).with_for_update()):
        ob.data = {
            **ob.data,
            "items": [
                {**i, "binding_id": new.id, "binding_version": new.version} if i["binding_id"] == old.id else i
                for i in ob.data["items"]
            ],
        }
        ob.binding_id = new.id
        ob.version += 1
    for p in db.scalars(
        select(Packet)
        .where((Packet.source_binding_id == old.id) | (Packet.topic_binding_id == old.id))
        .with_for_update()
    ):
        if p.source_binding_id == old.id:
            p.source_binding_id = new.id
        if p.topic_binding_id == old.id:
            p.topic_binding_id = new.id
        p.version += 1


def retry_publication(db, actor, publication_id, expected, data):
    pub = db.get(PacketPublication, publication_id)
    if not pub:
        raise BusinessError("Публикация не найдена.")
    r = db.get(PacketRevision, pub.revision_id)
    p = get(db, r.packet_id, True)
    authorize_edit(db, actor, p, r)
    versioned(pub, expected)
    retried = []
    for ident in pub.data["outbox_ids"]:
        row = db.get(Outbox, ident)
        if row.status in {"sent", "pending"}:
            continue
        if row.status in {"sending", "uncertain"}:
            raise BusinessError("Сначала сверьте фактическую доставку неопределённой части; повтор вслепую запрещён.")
        if row.status == "cancelled":
            continue
        deliverable(db, row)
        row.status = "pending"
        row.retry_at = now()
        row.error = None
        retried.append(row.id)
    pub.version += 1
    stamp(db, actor, pub, "PACKET_FAILED_PARTS_RETRIED", data={"outbox_ids": retried})
    return {"id": pub.id, "version": pub.version, "retried": len(retried)}


def control_tick(db):
    ob = db.scalar(
        select(PacketObligation)
        .join(PacketRevision)
        .join(Packet, Packet.id == PacketRevision.packet_id)
        .where(
            Packet.bot_id == settings().bot_id, PacketObligation.status == "pending", PacketObligation.next_at <= now()
        )
        .order_by(PacketObligation.next_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if not ob:
        return False
    r = db.get(PacketRevision, ob.revision_id)
    p = get(db, r.packet_id)
    current = status(db, ob)
    if current in {"accepted", "cancelled"}:
        ob.status = current
        ob.version += 1
        return True
    ob.next_at = now() + timedelta(minutes=5)
    if current == "review" or not ob.data.get("delivered_at"):
        return False
    cal = Calendar(r.audience["calendar"])
    if not ob.data.get("overdue_signalled"):
        try:
            binding = support_route(
                db, {"partner_id": ob.partner_id, "brand_id": ob.brand_id, "establishment_id": ob.establishment_id}
            )
        except BusinessError as error:
            ob.data = {**ob.data, "control_error": str(error)}
            return True
        queue(
            db,
            binding,
            f"packet-overdue:{ob.id}:{ob.version}",
            "Срок обязательства пакета нарушен.\n" + p.title + "\nОбязательство: " + ob.id + "\nФП: " + ob.partner_id,
        )
        ob.data = {**ob.data, "overdue_signalled": now().isoformat()}
    reminder_days = r.data.get("remind_days", 0)
    if reminder_days:
        last = ob.data.get("last_reminder")
        reserved = db.get(Outbox, ob.data.get("reminder_outbox")) if ob.data.get("reminder_outbox") else None
        base = datetime.fromisoformat(last) if last else ob.due_at
        if not reserved and cal.shift_workdays(base, reminder_days) <= now():
            # One primary connected presentation for a common obligation, not one per brand.
            b = db.get(Binding, ob.binding_id)
            if not b or not b.active or not b.verified:
                candidates = []
                for pub in db.scalars(
                    select(PacketPublication).where(
                        PacketPublication.revision_id == r.id, PacketPublication.delivered_at.is_not(None)
                    )
                ):
                    target = db.get(Binding, pub.binding_id)
                    if (
                        target
                        and target.active
                        and target.verified
                        and any(scope_key(r.data, i) == ob.scope_key for i in pub.data["items"])
                    ):
                        candidates.append(target)
                b = candidates[0] if candidates else None
            if b:
                ob.binding_id = b.id
                queue_reminder_group(db, r, ob, b, cal, reminder_days)
    escalate_days = r.data.get("escalate_days", 0)
    if (
        escalate_days
        and not ob.data.get("management_signalled")
        and cal.shift_workdays(ob.due_at, escalate_days) <= now()
    ):
        # Reuse durable management controls for a task; acknowledgements retain a separate explicit signal.
        b = next(
            (
                x
                for x in db.scalars(
                    select(Binding).where(
                        Binding.bot_id == p.bot_id,
                        Binding.purpose == "management",
                        Binding.active.is_(True),
                        Binding.verified.is_(True),
                    )
                )
                if all(
                    not getattr(x, k) or getattr(x, k) == getattr(ob, k)
                    for k in ("partner_id", "brand_id", "establishment_id")
                )
            ),
            None,
        )
        if b:
            queue(
                db,
                b,
                f"packet-management:{ob.id}:{ob.version}",
                "Просрочено обязательство пакета.\n" + p.title + "\nОбязательство: " + ob.id + "\nФП: " + ob.partner_id,
            )
            ob.data = {**ob.data, "management_signalled": now().isoformat()}
    return True


def schedule(db, actor, data, key):
    start = parse_date(data["start_at"])
    until = parse_date(data["until_at"])
    if start <= now() or until < start or data.get("repeat") not in {"weekly", "monthly"}:
        raise BusinessError("Нужны будущий старт, конец периода и еженедельное/ежемесячное повторение.")
    payload = deepcopy(data["payload"])
    calendar = db.scalar(
        select(Record).where(
            Record.kind == "calendar", Record.key == payload.get("calendar", "support"), Record.published.is_(True)
        )
    )
    if not calendar:
        raise BusinessError("Для расписания нужен опубликованный календарь.")
    cal = Calendar(calendar.data)
    payload["apply_at"] = data["start_at"]
    if payload["mode"] != "information":
        days = data.get("due_workdays")
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
            raise BusinessError("Задайте срок каждого цикла, 1–365 рабочих дней.")
        payload["due_at"] = cal.end_of_workday(start, days).isoformat() + "+00:00"
    snap = audience(db, actor, payload)
    local = start.replace(tzinfo=timezone.utc).astimezone(cal.tz)
    if not db.get(PacketSchedule, key):
        db.add(
            PacketSchedule(
                id=key,
                bot_id=settings().bot_id,
                author_id=actor.person_id,
                binding_id=actor.binding_id,
                next_at=start,
                until_at=until,
                data={
                    **deepcopy(data),
                    "payload": payload,
                    "material_approval": snap["materials"],
                    "assets": snap["assets"],
                    "grants": snap["author_grants"],
                    "calendar": calendar.data,
                    "anchor_day": local.day,
                    "cycle": 0,
                },
            )
        )
        db.flush()
    return {
        "id": key,
        "fingerprint": snap["fingerprint"],
        "audience": snap,
        "next_at": start.isoformat(),
        "message": "Каждый будущий цикл фиксирует свою аудиторию и отдельные обязательства.",
    }


def schedule_tick(db):
    s = db.scalar(
        select(PacketSchedule)
        .where(
            PacketSchedule.bot_id == settings().bot_id,
            PacketSchedule.enabled.is_(True),
            PacketSchedule.next_at <= now(),
        )
        .order_by(PacketSchedule.next_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if not s:
        return False
    if s.until_at < now():
        s.enabled = False
        s.data = {**s.data, "error": "Период закончился; прошлые пакеты не создаются"}
        return True
    if s.data.get("error"):
        return False
    actor = Actor(s.author_id, s.binding_id)
    try:
        with db.begin_nested():
            if access.grants_hash(db, actor) != s.data["grants"]:
                raise BusinessError("Назначения автора изменились; подтвердите расписание заново.")
            payload = deepcopy(s.data["payload"])
            cal = Calendar(s.data["calendar"])
            scheduled = s.next_at
            local = scheduled.replace(tzinfo=timezone.utc).astimezone(cal.tz)
            today = now().replace(tzinfo=timezone.utc).astimezone(cal.tz).date()
            # No burst of old campaigns or silent past deadlines after an outage.
            if local.date() < today:
                s.next_at = next_cycle(s, scheduled)
                s.data = {**s.data, "skipped": s.data.get("skipped", 0) + 1}
                return True
            payload["apply_at"] = scheduled.isoformat() + "+00:00"
            if payload["mode"] != "information":
                payload["due_at"] = cal.end_of_workday(scheduled, s.data["due_workdays"]).isoformat() + "+00:00"
            mats, assets = materials(db, payload, actor)
            if mats != s.data["material_approval"] or assets != s.data["assets"]:
                raise BusinessError("Материалы расписания изменились; нужна новая проверка.")
            key = sha256(f"packet-schedule:{s.id}:{scheduled.isoformat()}".encode()).hexdigest()[:32]
            create(db, actor, payload, key)
            s.data = {**s.data, "cycle": s.data.get("cycle", 0) + 1, "last_packet_id": key}
            s.next_at = next_cycle(s, scheduled)
            s.enabled = s.next_at <= s.until_at
    except BusinessError as error:
        s.enabled = False
        s.data = {**s.data, "error": str(error)}
    return True


def next_cycle(s, current):
    cal = Calendar(s.data["calendar"])
    local = current.replace(tzinfo=timezone.utc).astimezone(cal.tz)
    if s.data["repeat"] == "weekly":
        return (local + timedelta(days=7)).astimezone(timezone.utc).replace(tzinfo=None)
    month = 1 if local.month == 12 else local.month + 1
    year = local.year + (local.month == 12)
    return (
        local.replace(year=year, month=month, day=min(s.data["anchor_day"], monthrange(year, month)[1]))
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )


def stop_schedule(db, actor, ident, expected, data):
    s = db.scalar(
        select(PacketSchedule)
        .where(PacketSchedule.id == ident, PacketSchedule.bot_id == settings().bot_id)
        .with_for_update()
    )
    if not s or s.author_id != actor.person_id:
        raise AccessDenied("Расписание принадлежит другому автору.")
    publisher(db, actor)
    versioned(s, expected)
    if not data.get("reason"):
        raise BusinessError("Укажите причину остановки.")
    s.enabled = False
    s.version += 1
    s.data = {**s.data, "stop_reason": data["reason"]}
    stamp(db, actor, s, "PACKET_SCHEDULE_STOPPED", data={"reason": data["reason"]})
    return {"id": s.id, "version": s.version}


def message_parts(text, limit=3500):
    parts = []
    current = []
    units = 0
    for c in text:
        size = 2 if ord(c) > 0xFFFF else 1
        if units + size > limit:
            parts.append("".join(current))
            current = []
            units = 0
        current.append(c)
        units += size
    if current:
        parts.append("".join(current))
    return parts or [""]


def reschedule_obligation(db, actor, ident, expected, data):
    source = db.get(PacketObligation, ident)
    if not source:
        raise BusinessError("Обязательство не найдено.")
    r = db.get(PacketRevision, source.revision_id)
    p = get(db, r.packet_id, True)
    ob = db.scalar(
        select(PacketObligation)
        .where(PacketObligation.id == ident)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    versioned(ob, expected)
    if actor.person_id != ob.curator_id or not access.scope(db, actor, ob, {"curator", "support_head"}):
        raise AccessDenied("Будущий срок назначает закреплённый куратор в сопровождении.")
    if status(db, ob) in {"acknowledged", "inherited", "accepted", "cancelled"}:
        raise BusinessError("Завершённое подтверждение не переписывается.")
    at = parse_date(data["due_at"])
    if at <= now() or not data.get("reason"):
        raise BusinessError("Нужны будущий срок и причина явного согласования.")
    history = ob.data.get("deadline_history", []) + [
        {"previous": ob.due_at.isoformat(), "due_at": at.isoformat(), "by": actor.person_id, "reason": data["reason"]}
    ]
    ob.data = {
        **ob.data,
        "deadline_history": history,
        "overdue_signalled": None,
        "management_signalled": None,
        "last_reminder": None,
    }
    ob.due_at = ob.next_at = at
    ob.status = "pending"
    ob.version += 1
    if ob.work_id:
        w = db.scalar(
            select(Work).where(Work.id == ob.work_id).with_for_update().execution_options(populate_existing=True)
        )
        if w.status == "done":
            raise BusinessError("Завершённый результат не переносится.")
        if w.data.get("paid_started_at") or w.data.get("procurement_ordered"):
            raise BusinessError("Заказанный результат переносится через отдельное согласование.")
        w.due_at = at
        w.version += 1
        from ama.sla import refresh_deadline, case_for, invalidate

        refresh_deadline(db, w, "packet_deadline_agreed")
        c = case_for(db, w, True)
        if c:
            invalidate(db, c, w, "result", "before", "day")
    row = queue(
        db,
        db.get(Binding, ob.binding_id),
        f"packet-new-deadline:{ob.id}:{ob.version}",
        "Согласован новый срок: " + p.title + "\n" + at.isoformat() + " UTC\nОснование: " + data["reason"],
        buttons=[["Открыть пакет", "af:pack:" + r.id]],
    )
    row.payload = {**row.payload, "partner_window": True}
    stamp(db, actor, ob, "PACKET_DEADLINE_AGREED", data={"reason": data["reason"], "due_at": at.isoformat()})
    return {"id": ob.id, "version": ob.version}


def after_transition(db, actor, w, action, data):
    if w.kind != "packet_task":
        return
    ob = db.scalar(select(PacketObligation).where(PacketObligation.work_id == w.id))
    if not ob:
        return
    if action == "reject":
        ob.data = {
            **ob.data,
            "deadline_history": ob.data.get("deadline_history", [])
            + [
                {
                    "previous": ob.due_at.isoformat(),
                    "due_at": w.due_at.isoformat(),
                    "by": actor.person_id,
                    "reason": data["reason"],
                    "source": "rework",
                }
            ],
            "overdue_signalled": None,
            "management_signalled": None,
            "last_reminder": None,
        }
        ob.due_at = ob.next_at = w.due_at
        ob.version += 1
        from ama.sla import case_for, invalidate

        c = case_for(db, w, True)
        if c:
            invalidate(db, c, w, "result", "before", "day")
    if w.status == "done":
        ob.status = "accepted" if w.outcome == "accepted" else "cancelled"
        ob.version += 1
        ob.data = {**ob.data, "completed_by": actor.person_id, "completed_at": now().isoformat()}
        r = db.get(PacketRevision, ob.revision_id)
        refresh_summary(db, get(db, r.packet_id), "result:" + ob.id + ":" + str(ob.version))


def report(db, actor, packet_id):
    p = get(db, packet_id)
    r = revision(db, p)
    from types import SimpleNamespace

    items = [
        i
        for i in r.audience["included"]
        if access.scope(
            db,
            actor,
            SimpleNamespace(**{k: i.get(k) for k in ("partner_id", "brand_id", "establishment_id")}),
            {"curator", "support_head", "marketing_manager"},
        )
    ]
    if not items:
        raise AccessDenied("В отчёте нет получателей вашей области.")
    keys = {scope_key(r.data, i) for i in items}
    obs = [
        o
        for o in db.scalars(select(PacketObligation).where(PacketObligation.revision_id == r.id))
        if o.scope_key in keys
    ]
    publications = [
        x
        for x in db.scalars(select(PacketPublication).where(PacketPublication.revision_id == r.id))
        if any(
            (i["partner_id"], i["establishment_id"], i["brand_id"])
            in {(j["partner_id"], j["establishment_id"], j["brand_id"]) for j in x.data["items"]}
            for i in items
        )
    ]
    columns = [
        "Пакет",
        "Редакция",
        "Тип строки",
        "Получатель / область",
        "Доставка",
        "Обязательство",
        "Срок UTC",
        "Подтверждено / результат UTC",
        "Work ID",
    ]
    rows = [
        [
            p.title,
            r.number,
            "Публикация",
            pub.binding_id,
            publication_state(db, pub),
            "",
            "",
            pub.delivered_at.isoformat() if pub.delivered_at else "",
            "",
        ]
        for pub in publications
    ]
    rows += [
        [
            p.title,
            r.number,
            "Обязательство",
            o.scope_key,
            "delivered" if o.data.get("delivered_at") else "not_delivered",
            status(db, o),
            o.due_at.isoformat(),
            o.data.get("acknowledged_at") or o.data.get("completed_at") or "",
            o.work_id or "",
        ]
        for o in obs
    ]
    return columns, rows


def answer(db, actor, work_id, expected, data, key):
    from ama.auth import require

    w = db.scalar(select(Work).where(Work.id == work_id).with_for_update().execution_options(populate_existing=True))
    if not w or w.kind != "packet_question":
        raise BusinessError("Вопрос по пакету не найден.")
    require(db, actor, STAFF, w)
    versioned(w, expected)
    if actor.person_id != w.owner_id or w.status in {"review", "done"}:
        raise AccessDenied("Ответ отправляет текущий исполнитель вопроса в рабочей теме.")
    if (
        not data.get("text")
        or len(data["text"]) > 2500
        or not data.get("next_check")
        or parse_date(data["next_check"]) <= now()
    ):
        raise BusinessError("Нужны текст ответа и будущая дата следующего контроля.")
    if w.status == "new":
        from ama.service import transition

        transition(db, actor, w.id, w.version, "take", {}, key + ":take")
    row = queue(
        db,
        db.get(Binding, w.client_binding_id),
        "packet-answer:" + key,
        data["text"],
        work=w,
        buttons=[["Вопросы по пакету", "af:pack:" + w.data["packet_revision_id"]]],
    )
    row.payload = {
        **row.payload,
        "packet_answer": w.id,
        "packet_answer_actor": actor.person_id,
        "packet_answer_binding": actor.binding_id,
        "packet_answer_grants": access.grants_hash(db, actor),
        "next_check": data["next_check"],
        "partner_window": True,
    }
    stamp(db, actor, w, "PACKET_ANSWER_QUEUED", data={"outbox_id": row.id, "text": data["text"]})
    return {
        "id": row.id,
        "work_id": w.id,
        "version": w.version,
        "message": "Ответ поставлен в очередь. Доставка фиксируется отдельно.",
    }


def answer_delivered(db, row):
    w = db.scalar(
        select(Work)
        .where(Work.id == row.payload["packet_answer"])
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not w:
        return
    history = list(w.data.get("public_answers", []))
    if any(x["outbox_id"] == row.id for x in history):
        return
    history.append(
        {
            "outbox_id": row.id,
            "text": row.payload["text"],
            "at": now().isoformat(),
            "by": row.payload["packet_answer_actor"],
        }
    )
    w.data = {**w.data, "public_answers": history}
    w.version += 1
    from ama.sla import case_for, preserve_overdue, planned

    c = case_for(db, w, True)
    at = parse_date(row.payload["next_check"])
    if c and w.owner_id == row.payload["packet_answer_actor"] and at > now() and w.status != "done":
        preserve_overdue(db, c, w)
        planned(db, c, w, at)
        w.due_at = at
    stamp(db, None, w, "PACKET_ANSWER_DELIVERED", data={"outbox_id": row.id})


def reminder_open(db, row):
    ids = row.payload.get("packet_reminder_ids") or [row.payload["packet_reminder"]]
    b = db.get(Binding, row.binding_id)
    if not b or not b.active or not b.verified:
        return []
    return [
        o
        for o in db.scalars(select(PacketObligation).where(PacketObligation.id.in_(ids)))
        if status(db, o) not in {"acknowledged", "inherited", "accepted", "cancelled", "review", "delivery_issue"}
        and o.binding_id == b.id
        and o.data.get("delivered_at")
        and active_person_role(
            db,
            o.person_id,
            {"partner"},
            SimpleNamespace(partner_id=o.partner_id, brand_id=b.brand_id, establishment_id=o.establishment_id),
        )
    ]


def reminder_text(db, r, obs):
    names = [
        (db.get(Entity, o.establishment_id).name if o.establishment_id else db.get(Entity, o.partner_id).name)[:50]
        for o in obs
    ]
    return (
        "Открытые обязательства: "
        + r.data["title"]
        + f"\nПунктов: {len(obs)}\n"
        + "\n".join("• " + x for x in names[:20])
        + ("\nОстальные — в карточке пакета." if len(names) > 20 else "")
    )


def queue_reminder_group(db, r, ob, b, cal, days):
    cohort = []
    for other in db.scalars(
        select(PacketObligation)
        .where(
            PacketObligation.revision_id == r.id,
            PacketObligation.binding_id == b.id,
            PacketObligation.person_id == ob.person_id,
            PacketObligation.status == "pending",
        )
        .order_by(PacketObligation.id)
    ):
        if (
            status(db, other) in {"accepted", "cancelled", "review", "delivery_issue"}
            or not other.data.get("delivered_at")
            or other.data.get("reminder_outbox")
        ):
            continue
        base = datetime.fromisoformat(other.data["last_reminder"]) if other.data.get("last_reminder") else other.due_at
        if cal.shift_workdays(base, days) <= now():
            cohort.append(other)
    if not cohort:
        return
    key = digest([(o.id, o.version, o.data.get("reminder_cycle", 0)) for o in cohort])[:24]
    row = queue(
        db,
        b,
        f"packet-reminder:{r.id}:{key}",
        reminder_text(db, r, cohort),
        buttons=[["Открыть пакет", "af:pack:" + r.id]],
    )
    row.payload = {
        **row.payload,
        "packet_reminder": cohort[0].id,
        "packet_reminder_ids": [o.id for o in cohort],
        "packet_revision": r.id,
        "partner_window": True,
    }
    db.flush()
    for other in cohort:
        other.data = {**other.data, "reminder_outbox": row.id}


def refresh_summary(db, p, event_key):
    binding = db.get(Binding, p.topic_binding_id) if p.topic_binding_id else None
    if not binding or not binding.active or not binding.verified:
        return
    counts = summary(db, p)
    queue(
        db,
        binding,
        f"packet-summary:{p.id}:{event_key}",
        p.title
        + f"\nРедакция {counts['revision']}: принято {counts['accepted']} из {counts['obligations']}. Доставлено чатов {counts['sent']} из {counts['chats']}.",
        buttons=[["Сводка пакета", "af:pack:" + counts["revision_id"]]],
    )
