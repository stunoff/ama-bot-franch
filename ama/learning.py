"""Personal learning, immutable content editions and delivery-anchored reminders."""

from copy import deepcopy
from types import SimpleNamespace
from datetime import timedelta, datetime
from urllib.parse import urlsplit
from sqlalchemy import select
from ama.models import (
    LearningTrack,
    LearningEdition,
    MaterialUse,
    Training,
    Record,
    Binding,
    Work,
    Entity,
    Outbox,
    EstablishmentBrand,
    DigitalProgram,
    now,
)
from ama.config import settings
from ama.errors import BusinessError, AccessDenied, Conflict
from ama.auth import Actor, roles_for, active_person_role
from ama.service import queue, parse_date, versioned


def material(db, key):
    rec = db.scalar(select(Record).where(Record.kind == "material", Record.key == key))
    seen = set()
    while rec and not rec.published and rec.data.get("replacement_key"):
        if rec.key in seen:
            raise BusinessError("Циклическая замена материала.")
        seen.add(rec.key)
        rec = db.scalar(select(Record).where(Record.kind == "material", Record.key == rec.data["replacement_key"]))
    if not rec or not rec.published or rec.data.get("availability", "active") not in {"active", "updating"}:
        raise BusinessError("Обязательный материал недоступен: " + key)
    u = urlsplit(rec.data.get("url", ""))
    if u.scheme != "https" or not u.hostname or u.username or u.password:
        raise BusinessError("Материал не имеет проверенной HTTPS-ссылки.")
    return rec


def can_material(db, actor, rec, *, lesson=False, establishment_id=None):
    b = db.get(Binding, actor.binding_id)
    d = rec.data
    roles = roles_for(db, actor)
    scope = SimpleNamespace(partner_id=b.partner_id, brand_id=b.brand_id, establishment_id=b.establishment_id)
    if establishment_id:
        est = db.get(Entity, establishment_id)
        if (
            not est
            or est.kind != "establishment"
            or est.partner_id != b.partner_id
            or b.establishment_id not in {None, est.id}
            or not db.scalar(
                select(EstablishmentBrand.id).where(
                    EstablishmentBrand.establishment_id == est.id,
                    EstablishmentBrand.brand_id == b.brand_id,
                    EstablishmentBrand.active.is_(True),
                )
            )
        ):
            return False
        scope.establishment_id = est.id
    audience = set(d.get("audience", []))
    if b.purpose == "client":
        audience &= {"partner", "observer"}
        if d.get("internal_only") or not active_person_role(db, actor.person_id, audience, scope):
            return False
    elif not roles.intersection(audience):
        return False
    if any(d.get(k) and d[k] != getattr(scope, k) for k in ("partner_id", "brand_id", "establishment_id")):
        return False
    if d.get("brand_id") and b.purpose == "client":
        links = list(
            db.scalars(
                select(EstablishmentBrand).where(
                    EstablishmentBrand.brand_id == b.brand_id, EstablishmentBrand.active.is_(True)
                )
            )
        )
        if not any(db.get(Entity, x.establishment_id).partner_id == b.partner_id for x in links):
            return False
    if d.get("call_center_only"):
        from ama.digital import value

        programs = list(db.scalars(select(DigitalProgram).where(DigitalProgram.bot_id == settings().bot_id)))
        applicable = [
            p
            for p in programs
            if db.get(Entity, p.establishment_id).partner_id == b.partner_id
            and b.brand_id in p.data.get("brands", [])
            and (not scope.establishment_id or p.establishment_id == scope.establishment_id)
        ]
        states = [value(db, p, "call_center") for p in applicable]
        if scope.establishment_id and applicable:
            if True not in states:
                return False
        elif True not in states:
            partner = db.get(Entity, b.partner_id)
            if not partner or not partner.data.get("call_center_applicable"):
                return False
    if b.purpose == "client" and not lesson:
        stage = d.get("access_stage")
        if stage == "general_completed" and not completed(db, actor.person_id):
            return False
        if (
            d.get("learning_managed")
            and stage == "current"
            and not d.get("brand_id")
            and not d.get("call_center_only")
            and not completed(db, actor.person_id)
        ):
            t = selected_training(db, actor.person_id, "general")
            if not t or d.get("module") != t.cards[t.position].get("module", 0):
                return False
    return True


def resolve(db, actor, key, *, lesson=False, establishment_id=None):
    rec = material(db, key)
    if not can_material(db, actor, rec, lesson=lesson, establishment_id=establishment_id):
        raise AccessDenied("Материал не разрешён в текущем чате / для этой роли.")
    return rec


def show_material(db, actor, key, *, lesson=False, establishment_id=None):
    rec = resolve(db, actor, key, lesson=lesson, establishment_id=establishment_id)
    use = MaterialUse(
        person_id=actor.person_id,
        binding_id=actor.binding_id,
        record_id=rec.id,
        record_version=rec.version,
        data={
            "url": rec.data["url"],
            "title": rec.data.get("title", rec.key),
            "fact": "queued_not_read",
            "requested_key": key,
        },
    )
    db.add(use)
    db.flush()
    row = queue(
        db,
        db.get(Binding, actor.binding_id),
        "learning-material:" + use.id,
        rec.data.get("title", rec.key) + "\n" + rec.data["url"],
    )
    row.payload = {
        **row.payload,
        "learning_material": {
            "record_id": rec.id,
            "version": rec.version,
            "person_id": actor.person_id,
            "binding_id": actor.binding_id,
            "lesson": lesson,
            "establishment_id": establishment_id,
            "use_id": use.id,
        },
    }
    return use


def check_card(db, actor, card):
    for key in card.get("required_materials", []):
        resolve(db, actor, key, lesson=True)


def selected_training(db, person, course):
    active = list(
        db.scalars(
            select(LearningTrack)
            .where(
                LearningTrack.bot_id == settings().bot_id,
                LearningTrack.person_id == person,
                LearningTrack.course_key == course,
                LearningTrack.status.in_(["pending", "blocked"]),
            )
            .order_by(LearningTrack.id)
        )
    )
    # Finish an already assigned full route before short update notices.
    active.sort(key=lambda x: x.kind == "update")
    if active:
        return db.get(Training, active[0].training_id)
    return db.scalar(
        select(Training)
        .where(Training.person_id == person, Training.course_key == course)
        .order_by(Training.course_version.desc())
    )


def track_for(db, t):
    return db.scalar(select(LearningTrack).where(LearningTrack.training_id == t.id))


def ensure_track(db, actor, t, kind="general"):
    old = track_for(db, t)
    if old:
        return old
    tr = LearningTrack(
        bot_id=settings().bot_id,
        person_id=t.person_id,
        course_key=t.course_key,
        training_id=t.id,
        binding_id=actor.binding_id,
        origin_key="learning:" + t.id,
        kind=kind,
        status="completed" if t.completed_at else "pending",
        data={"modules": [], "round": 0, "delivered_at": None, "last_action": None, "history": []},
    )
    db.add(tr)
    db.flush()
    return tr


def completed(db, person, course="general"):
    if db.scalar(
        select(LearningTrack.id).where(
            LearningTrack.bot_id == settings().bot_id,
            LearningTrack.person_id == person,
            LearningTrack.course_key == course,
            LearningTrack.status.in_(["pending", "blocked"]),
        )
    ):
        return False
    return bool(
        db.scalar(
            select(Training.id).where(
                Training.person_id == person, Training.course_key == course, Training.completed_at.is_not(None)
            )
        )
    ) or bool(
        db.scalar(
            select(LearningTrack.id).where(
                LearningTrack.person_id == person,
                LearningTrack.course_key == course,
                LearningTrack.status == "inherited",
            )
        )
    )


def action(db, actor, course, action="open", expected=None):
    from ama.service import training_action

    t = training_action(db, actor, course, "open", expected)
    tr = ensure_track(db, actor, t)
    tr = db.scalar(
        select(LearningTrack)
        .where(LearningTrack.id == tr.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    # opening increments only the screen version; checks refer to the version before that read.
    if expected is not None and expected != t.version - 1:
        raise Conflict("Карточка изменилась. Откройте актуальную.")
    if t.completed_at or tr.status == "inherited":
        if action == "back":
            t.position = max(0, t.position - 1)
        elif action == "next":
            t.position = min(len(t.cards) - 1, t.position + 1)
        elif action == "materials":
            for key in t.cards[t.position].get("required_materials", []):
                show_material(db, actor, key, lesson=True)
        return t
    if action in {"next", "finish_module", "finish"}:
        check_card(db, actor, t.cards[t.position])
        module = t.cards[t.position].get("module", 0)
        boundary = t.position == len(t.cards) - 1 or t.cards[t.position + 1].get("module", 0) != module
        if action == "next" and boundary:
            raise BusinessError("Подтвердите завершение раздела отдельной кнопкой.")
        if action == "finish_module" and not boundary:
            raise BusinessError("Сначала последовательно просмотрите карточки раздела.")
        if action == "finish":
            if not tr.data.get("await_finish") or t.position != len(t.cards) - 1:
                raise BusinessError("Сначала завершите все назначенные разделы.")
            t.completed_at = now()
            tr.status = "completed"
        else:
            t.acknowledged = sorted(set([*t.acknowledged, t.position]))
            if action == "finish_module":
                tr.data = {**tr.data, "modules": sorted(set([*tr.data.get("modules", []), module]))}
            if t.position < len(t.cards) - 1:
                t.position += 1
            else:
                tr.data = {**tr.data, "await_finish": True}
        tr.data = {
            **tr.data,
            "last_action": (now().isoformat() + "Z"),
            "round": 0,
            "reservation": None,
            "history": [
                *tr.data.get("history", []),
                {"action": action, "person": actor.person_id, "at": (now().isoformat() + "Z"), "position": t.position},
            ],
        }
        tr.version += 1
        t.paused = False
        cancel_stale(db, tr)
    elif action == "back":
        t.position = max(0, t.position - 1)
    elif action == "later":
        t.paused = True
    elif action not in {"open", "materials"}:
        raise BusinessError("Неизвестное действие ознакомления.")
    if action == "materials":
        for key in t.cards[t.position].get("required_materials", []):
            show_material(db, actor, key, lesson=True)
    return t


def cancel_stale(db, tr):
    for row in db.scalars(select(Outbox).where(Outbox.status.in_(["pending", "failed", "blocked"]))):
        ctx = row.payload.get("learning_reminder")
        if ctx and ctx["track"] == tr.id:
            row.status = "cancelled"


def edition(db, rec, data, version, published, actor=None):
    if db.scalar(
        select(LearningEdition.id).where(LearningEdition.record_id == rec.id, LearningEdition.record_version == version)
    ):
        return
    db.add(
        LearningEdition(
            record_id=rec.id,
            record_version=version,
            data={
                "content": deepcopy(data),
                "published": published,
                "actor": actor.person_id if actor else None,
                "at": (now().isoformat() + "Z"),
            },
        )
    )


def on_record(db, actor, rec, previous=None):
    if rec.kind not in {"material", "course"}:
        return
    if previous:
        edition(db, rec, previous["data"], previous["version"], previous["published"], actor)
    if rec.published:
        if rec.data.get("substantial_update"):
            cards = rec.data.get("update_cards")
            if not isinstance(cards, list) or not cards:
                raise BusinessError("Нужны отдельные карточки изменений.")
            for card in cards:
                if (
                    not isinstance(card, dict)
                    or not card.get("title")
                    or not card.get("text")
                    or not isinstance(card.get("module", 0), int)
                    or not 0 <= card.get("module", 0) <= 11
                ):
                    raise BusinessError("Некорректная карточка изменений.")
                for key in card.get("required_materials", []):
                    material(db, key)
        if rec.kind == "material":
            rec.data = {
                **rec.data,
                "learning_managed": True,
                "access_stage": rec.data.get("access_stage")
                or ("current" if rec.data.get("brand_id") or rec.data.get("call_center_only") else "general_completed"),
            }
        if not rec.data.get("content_approval"):
            raise BusinessError("Нужно подтверждение владельца содержания.")
        if rec.kind == "material":
            if not rec.data.get("verified_on") or set(rec.data.get("link_checks", [])) != {
                "opens",
                "audience",
                "exists",
                "title",
            }:
                raise BusinessError(
                    "Подтвердите дату и четыре проверки ссылки: открывается, права, существование, название."
                )
            from datetime import date

            if date.fromisoformat(rec.data["verified_on"]) > now().date():
                raise BusinessError("Дата проверки не может быть в будущем.")
        if rec.kind == "course" and rec.key == "general" and len(rec.data.get("cards", [])) > 100:
            modules = {c.get("module", 0) for c in rec.data["cards"] if c.get("required_materials")}
            if modules != set(range(12)):
                raise BusinessError(
                    "Полный маршрут требует настроенные обязательные материалы разделов 0–11; используйте реестр готовности контента."
                )
        if rec.kind == "course" and rec.data.get("substantial_update"):
            if not rec.data.get("required_update_reason") or not rec.data.get("update_cards"):
                raise BusinessError("Существенное обновление требует причины и отдельных карточек изменений.")
    edition(db, rec, rec.data, rec.version, rec.published, actor)
    if rec.kind == "material":
        material_updates(db, actor, rec, previous)
    if rec.kind == "course" and rec.published and previous and rec.data.get("substantial_update"):
        for old in list(
            db.scalars(select(Training).where(Training.course_key == rec.key, Training.course_version < rec.version))
        ):
            bindings = list(
                db.scalars(
                    select(Binding).where(
                        Binding.bot_id == settings().bot_id,
                        Binding.purpose == "client",
                        Binding.active.is_(True),
                        Binding.verified.is_(True),
                    )
                )
            )
            target = next(
                (
                    b
                    for b in bindings
                    if active_person_role(db, old.person_id, {"partner"}, b)
                    and (not rec.data.get("update_brand_ids") or b.brand_id in rec.data["update_brand_ids"])
                ),
                None,
            )
            if not target:
                continue
            if db.scalar(
                select(Training.id).where(
                    Training.person_id == old.person_id,
                    Training.course_key == rec.key,
                    Training.course_version == rec.version,
                )
            ):
                continue
            t = Training(
                person_id=old.person_id,
                course_key=rec.key,
                course_version=rec.version,
                cards=deepcopy(rec.data["update_cards"]),
                position=0,
                acknowledged=[],
            )
            db.add(t)
            db.flush()
            tr = ensure_track(db, Actor(old.person_id, target.id), t, "update")
            row = queue(
                db,
                target,
                "learning-assignment:" + tr.id,
                "Обязательное обновление ознакомления: "
                + rec.data["required_update_reason"]
                + "\n/learning — только карточки изменений, без повторения общего курса.",
            )
            row.payload = {**row.payload, "learning_assignment": tr.id}


def assign(db, actor, data):
    b = db.get(Binding, data["binding_id"])
    person = data["person_id"]
    if not b or b.purpose != "client" or not b.active or not b.verified or b.bot_id != settings().bot_id:
        raise BusinessError("Нужен проверенный клиентский маршрут.")
    if db.get(Binding, actor.binding_id).purpose != "support" or not active_person_role(
        db, actor.person_id, {"curator"}, b
    ):
        raise AccessDenied("Назначает куратор в сопровождении.")
    if not active_person_role(db, person, {"partner"}, b):
        raise BusinessError("У человека нет действующей роли ФП.")
    from ama.models import Identity

    db.scalar(select(Identity).where(Identity.id == person).with_for_update())
    course = data.get("course_key", "general")
    rec = db.scalar(select(Record).where(Record.kind == "course", Record.key == course, Record.published.is_(True)))
    if not rec or not rec.data.get("cards"):
        raise BusinessError("Курс не опубликован.")
    if course == "call_center" and not can_material(
        db,
        Actor(person, b.id),
        SimpleNamespace(data={"audience": ["partner"], "call_center_only": True}),
        establishment_id=data.get("establishment_id"),
    ):
        raise BusinessError("Короткий курс доступен по подтверждённому обслуживанию КЦ предприятия.")
    t = selected_training(db, person, course)
    if not t:
        t = Training(
            person_id=person,
            course_key=course,
            course_version=rec.version,
            cards=deepcopy(rec.data["cards"]),
            position=0,
        )
        db.add(t)
        db.flush()
    tr = ensure_track(db, Actor(person, b.id), t)
    if data.get("mode") in {"inherit", "updates"}:
        if not data.get("evidence") or not data.get("known_version"):
            raise BusinessError(
                "Наследование требует подтверждённое основание и известную версию; неизвестное прохождение не выдумывается."
            )
        if tr.status not in {"completed", "inherited"}:
            tr.status = "inherited"
            tr.data = {
                **tr.data,
                "inheritance": {
                    "evidence": data["evidence"],
                    "known_version": data["known_version"],
                    "mode": data.get("mode"),
                    "actor": actor.person_id,
                    "at": (now().isoformat() + "Z"),
                },
            }
            tr.version += 1
        return tr
    if tr.status in {"completed", "inherited"}:
        return tr
    if not tr.data.get("assigned"):
        row = queue(
            db,
            b,
            "learning-assignment:" + tr.id,
            "Назначено персональное ознакомление: /learning. Оценок и экзаменов нет.",
        )
        row.payload = {**row.payload, "learning_assignment": tr.id}
        tr.data = {**tr.data, "assigned": True, "curator_id": actor.person_id}
    return tr


def report_problem(db, actor, key, text):
    rec = db.scalar(select(Record).where(Record.kind == "material", Record.key == key))
    if not rec or not can_material(db, actor, rec, lesson=True):
        raise AccessDenied("Материал недоступен.")
    old = rec.data.get("problem_work")
    if old and db.get(Work, old) and db.get(Work, old).status != "done":
        return db.get(Work, old)
    epoch = rec.data.get("problem_epoch", 0) + 1
    from ama.service import create_work

    w = create_work(
        db,
        actor,
        {
            "kind": "support",
            "title": "Проблема материала: " + rec.data.get("title", rec.key),
            "data": {"material_key": rec.key, "problem": text},
        },
        "material-problem:" + rec.id + ":" + str(rec.version) + ":" + str(epoch),
    )
    rec.data = {**rec.data, "availability": "blocked", "problem_work": w.id, "problem_epoch": epoch, "problem": text}
    return w


def archive(db, actor, rec, replacement=None, reason=None):
    edition(db, rec, rec.data, rec.version, rec.published, actor)
    if not reason:
        raise BusinessError("Нужно основание изменения доступности.")
    if replacement:
        target = material(db, replacement)
        if (
            target.id == rec.id
            or set(rec.data.get("audience", [])) - set(target.data.get("audience", []))
            or any(
                rec.data.get(k) != target.data.get(k)
                for k in (
                    "partner_id",
                    "brand_id",
                    "establishment_id",
                    "call_center_only",
                    "internal_only",
                    "access_stage",
                )
            )
        ):
            raise BusinessError("Замена должна сохранять аудиторию и область материала.")
        rec.data = {**rec.data, "replacement_key": target.key, "archive_reason": reason}
    else:
        if not rec.owner_id:
            raise BusinessError("Техническое ожидание требует ответственного владельца содержания.")
        rec.data = {**rec.data, "availability": "blocked", "archive_reason": reason, "technical_owner_id": rec.owner_id}
    rec.published = False
    rec.version += 1
    edition(db, rec, rec.data, rec.version, False, actor)


def tick(db):
    changed = False
    for rec in db.scalars(select(Record).where(Record.kind == "material", Record.published.is_(True))):
        days = rec.data.get("review_days")
        verified = rec.data.get("verified_on")
        if (
            days
            and verified
            and datetime.fromisoformat(verified) + timedelta(days=days) <= now()
            and not rec.data.get("review_notice")
        ):
            route = db.scalar(
                select(Record).where(Record.kind == "route", Record.key == "support", Record.published.is_(True))
            )
            target = db.get(Binding, route.data.get("binding_id")) if route else None
            if target:
                queue(
                    db,
                    target,
                    "material-review:" + rec.id + ":" + str(rec.version),
                    "Требуется повторная проверка ссылки: "
                    + rec.data.get("title", rec.key)
                    + " · владелец "
                    + str(rec.owner_id),
                )
                rec.data = {**rec.data, "review_notice": now().isoformat() + "Z"}
                changed = True
    for tr in db.scalars(
        select(LearningTrack).where(
            LearningTrack.bot_id == settings().bot_id, LearningTrack.status.in_(["pending", "blocked"])
        )
    ):
        tr = db.scalar(
            select(LearningTrack)
            .where(LearningTrack.id == tr.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        t = db.get(Training, tr.training_id)
        current = selected_training(db, tr.person_id, tr.course_key)
        if current and current.id != tr.training_id and not (t and t.completed_at):
            tr.data = {**tr.data, "awaiting_prior": True}
            continue
        if tr.data.get("awaiting_prior"):
            tr.data = {**tr.data, "awaiting_prior": False, "active_at": now().isoformat() + "Z"}
        if t and t.completed_at:
            tr.status = "completed"
            cancel_stale(db, tr)
            changed = True
            continue
        if not t:
            continue
        actor = Actor(tr.person_id, tr.binding_id)
        try:
            if not active_person_role(db, tr.person_id, {"partner"}, db.get(Binding, tr.binding_id)):
                continue
            check_card(db, actor, t.cards[t.position])
            if tr.status == "blocked":
                since = parse_date(tr.data["blocked_since"])
                duration = now() - since
                shifts = {
                    k: (parse_date(tr.data[k]) + duration).isoformat() + "Z"
                    for k in ("last_action", "last_reminder", "delivered_at")
                    if tr.data.get(k)
                }
                tr.data = {**tr.data, **shifts, "technical_blocker": None, "reservation": None}
                tr.version += 1
                changed = True
            tr.status = "pending"
        except BusinessError as e:
            if tr.data.get("technical_blocker") != str(e):
                tr.data = {**tr.data, "technical_blocker": str(e), "blocked_since": (now().isoformat() + "Z")}
                tr.status = "blocked"
                tr.data = {**tr.data, "reservation": None}
                tr.version += 1
                cancel_stale(db, tr)
                changed = True
            continue
        if opening_notice(db, tr):
            changed = True
            continue
        opening_row = db.get(Outbox, tr.data.get("opening_reservation")) if tr.data.get("opening_reservation") else None
        if opening_row and opening_row.status in {"pending", "sending", "uncertain"}:
            continue
        if tr.data.get("reservation"):
            reserved = db.get(Outbox, tr.data["reservation"])
            if reserved and reserved.status == "cancelled":
                tr.data = {**tr.data, "reservation": None}
                tr.version += 1
            else:
                continue
        round_ = tr.data.get("round", 0)
        if round_ >= 2:
            continue
        anchor = tr.data.get("last_reminder") if round_ else tr.data.get("last_action") or tr.data.get("delivered_at")
        if not anchor:
            continue
        if tr.data.get("active_at"):
            anchor = max(parse_date(anchor), parse_date(tr.data["active_at"])).isoformat() + "Z"
        due = parse_date(anchor) + (timedelta(days=3) if round_ else timedelta(days=2))
        if tr.data.get("manual_due"):
            due = max(due, parse_date(tr.data["manual_due"]))
        if due > now():
            continue
        b = db.get(Binding, tr.binding_id)
        from ama.worker import next_partner_window

        try:
            at = next_partner_window(db, b, now())
        except BusinessError:
            curator_task(db, tr, "Не настроено согласованное окно уведомлений ФП.")
            continue
        if at > now():
            continue
        row = queue(
            db,
            b,
            "learning-reminder:" + tr.id + ":" + str(tr.version) + ":" + str(round_),
            "Можно продолжить ознакомление с сохранённого места: /learning. Если нужна помощь, сообщите куратору.",
        )
        row.payload = {
            **row.payload,
            "learning_reminder": {"track": tr.id, "version": tr.version, "round": round_},
            "partner_window": True,
        }
        db.flush()
        tr.data = {**tr.data, "reservation": row.id}
        changed = True
    return changed


def curator_task(db, tr, reason):
    if tr.data.get("curator_work"):
        return
    b = db.get(Binding, tr.binding_id)
    route = db.scalar(select(Record).where(Record.kind == "route", Record.key == "support", Record.published.is_(True)))
    target = db.get(Binding, route.data.get("binding_id")) if route else None
    if not target:
        return
    curator = next(
        (
            person
            for person in (tr.data.get("curator_id"), route.data.get("reviewer_id"), route.data.get("owner_id"))
            if person and active_person_role(db, person, {"curator"}, b)
        ),
        None,
    )
    if not curator:
        return
    cal = db.scalar(
        select(Record).where(
            Record.kind == "calendar", Record.key == route.data.get("calendar"), Record.published.is_(True)
        )
    )
    if not cal:
        return
    from ama.calendar import Calendar

    due = Calendar(cal.data).end_of_workday(now(), 1)
    w = Work(
        kind="learning_control",
        title="Контроль персонального ознакомления",
        partner_id=b.partner_id,
        brand_id=b.brand_id,
        establishment_id=b.establishment_id,
        binding_id=target.id,
        client_binding_id=b.id,
        owner_id=curator,
        executor_id=curator,
        reviewer_id=curator,
        partner_person_id=tr.person_id,
        due_at=due,
        original_due_at=due,
        data={"calendar": route.data.get("calendar", "support"), "learning_track": tr.id, "reason": reason},
    )
    db.add(w)
    db.flush()
    from ama.sla import initialize

    initialize(db, w)
    tr.data = {**tr.data, "curator_work": w.id}
    queue(db, target, "learning-control:" + tr.id, reason + "\n" + w.id, work=w)


def delivered(db, row):
    opening = row.payload.get("learning_opening")
    if opening:
        tr = db.scalar(
            select(LearningTrack)
            .where(LearningTrack.id == opening["track"])
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if tr and tr.version == opening["version"]:
            tr.data = {
                **tr.data,
                "opening_notices": {**tr.data.get("opening_notices", {}), opening["key"]: now().isoformat() + "Z"},
                "opening_reservation": None,
            }

    if row.payload.get("learning_material"):
        use = db.get(MaterialUse, row.payload["learning_material"].get("use_id"))
        if use:
            use.data = {**use.data, "fact": "delivered_not_read", "delivered_at": now().isoformat() + "Z"}
    if row.payload.get("learning_assignment"):
        tr = db.scalar(
            select(LearningTrack)
            .where(LearningTrack.id == row.payload["learning_assignment"])
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if tr and not tr.data.get("delivered_at"):
            tr.data = {**tr.data, "delivered_at": (now().isoformat() + "Z")}
    ctx = row.payload.get("learning_reminder")
    if ctx:
        tr = db.scalar(
            select(LearningTrack)
            .where(LearningTrack.id == ctx["track"])
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if tr and tr.version == ctx["version"] and tr.data.get("reservation") == row.id:
            tr.data = {
                **tr.data,
                "last_reminder": (now().isoformat() + "Z"),
                "round": ctx["round"] + 1,
                "reservation": None,
            }
            if tr.data["round"] == 2:
                curator_task(db, tr, "Два напоминания доставлены; требуется личный контакт куратора.")


def deliverable(db, row):
    recipient_track = row.payload.get("learning_assignment") or (
        row.payload.get("learning_reminder") or row.payload.get("learning_opening") or {}
    ).get("track")
    if recipient_track:
        tr = db.get(LearningTrack, recipient_track)
        b = db.get(Binding, tr.binding_id) if tr else None
        if (
            not tr
            or tr.bot_id != settings().bot_id
            or not b
            or not b.active
            or not b.verified
            or not active_person_role(db, tr.person_id, {"partner"}, b)
        ):
            return False

    opening = row.payload.get("learning_opening")
    if opening:
        from ama.models import ProjectPlan

        tr = db.get(LearningTrack, opening["track"])
        p = db.get(ProjectPlan, opening["project"])
        if (
            not tr
            or tr.status != "pending"
            or tr.version != opening["version"]
            or not p
            or p.dates.get("planned_internal") != opening["date"]
            or p.dates.get("actual_internal")
        ):
            return False

    ctx = row.payload.get("learning_material")
    if ctx:
        rec = db.get(Record, ctx["record_id"])
        try:
            return bool(
                rec
                and rec.version == ctx["version"]
                and resolve(
                    db,
                    Actor(ctx["person_id"], ctx["binding_id"]),
                    rec.key,
                    lesson=ctx["lesson"],
                    establishment_id=ctx.get("establishment_id"),
                ).id
                == rec.id
            )
        except BusinessError:
            return False
    ctx = row.payload.get("learning_reminder")
    if ctx:
        tr = db.get(LearningTrack, ctx["track"])
        if not tr or tr.status != "pending" or tr.version != ctx["version"] or tr.data.get("reservation") != row.id:
            return False
        t = db.get(Training, tr.training_id)
        try:
            check_card(db, Actor(tr.person_id, tr.binding_id), t.cards[t.position])
        except BusinessError as error:
            tr.status = "blocked"
            tr.version += 1
            tr.data = {
                **tr.data,
                "technical_blocker": str(error),
                "blocked_since": now().isoformat() + "Z",
                "reservation": None,
            }
            return False
    return True


def authorize_draft(db, actor, draft):
    a = draft.kind.split(":")[1]
    if a == "prepare":
        from ama.admin import check_admin

        check_admin(db, actor)
    elif a == "archive":
        from ama.admin import check_admin

        check_admin(db, actor)
        obj = db.get(Record, draft.target_id)
        if not obj or obj.kind != "material":
            raise BusinessError("Нужна карточка материала.")
        versioned(obj, draft.target_version)
    elif a == "assign":
        b = db.get(Binding, actor.binding_id)
        if b.purpose != "support" or not active_person_role(db, actor.person_id, {"curator"}, b):
            raise AccessDenied("Назначает куратор в сопровождении.")
    elif a == "deadline":
        tr = db.get(LearningTrack, draft.target_id)
        if (
            not tr
            or tr.bot_id != settings().bot_id
            or db.get(Binding, actor.binding_id).purpose != "support"
            or not active_person_role(db, actor.person_id, {"curator"}, db.get(Binding, tr.binding_id))
        ):
            raise AccessDenied("Срок согласует куратор этой области.")
        versioned(tr, draft.target_version)
    elif a == "problem":
        rec = db.get(Record, draft.target_id)
        if not rec or not can_material(db, actor, rec, lesson=True):
            raise AccessDenied("Материал недоступен.")
        versioned(rec, draft.target_version)


def execute_draft(db, actor, draft, data):
    a = draft.kind.split(":")[1]
    if a == "prepare":
        return prepare_content(db, actor)
    if a == "assign":
        return {"id": assign(db, actor, data).id}
    if a == "deadline":
        tr = db.get(LearningTrack, draft.target_id)
        due = parse_date(data["due_at"])
        if due <= now() or not data.get("reason"):
            raise BusinessError("Нужны будущая дата и основание.")
        tr.data = {
            **tr.data,
            "manual_due": due.isoformat() + "Z",
            "reservation": None,
            "history": [
                *tr.data.get("history", []),
                {
                    "action": "manual_due",
                    "at": now().isoformat() + "Z",
                    "actor": actor.person_id,
                    "previous": tr.data.get("manual_due"),
                    "due": due.isoformat() + "Z",
                    "reason": data["reason"],
                },
            ],
        }
        tr.version += 1
        cancel_stale(db, tr)
        return {"id": tr.id}
    rec = db.get(Record, draft.target_id)
    if a == "problem":
        return {"id": report_problem(db, actor, rec.key, data["reason"]).id}
    if a == "archive":
        from ama.ui.configuration import dependencies

        deps = dependencies(db, rec)
        archive(db, actor, rec, data.get("replacement_key"), data["reason"])
        return {
            "id": rec.id,
            "dependencies": deps,
            "replacement": data.get("replacement_key"),
            "technical_wait": not bool(data.get("replacement_key")),
        }
    raise BusinessError("Неизвестное действие материала.")


def opening_notice(db, tr):
    if tr.course_key != "general":
        return False
    from ama.models import ProjectPlan

    candidates = []
    for p in db.scalars(
        select(ProjectPlan).where(
            ProjectPlan.bot_id == settings().bot_id, ProjectPlan.partner_person_id == tr.person_id
        )
    ):
        if p.dates.get("actual_internal") or not p.dates.get("planned_internal"):
            continue
        date = parse_date(p.dates["planned_internal"])
        remaining = date - now()
        if remaining <= timedelta(0) or remaining > timedelta(days=7):
            continue
        stage = 1 if remaining <= timedelta(days=1) else 3 if remaining <= timedelta(days=3) else 7
        candidates.append((stage, date, p.id, p.dates["planned_internal"]))
    if not candidates:
        return False
    stage, date, project, stamp = min(candidates)
    if tr.status == "blocked":
        if stage == 1:
            curator_task(db, tr, "Риск внутреннего открытия: технически недоступен обязательный материал.")
        return False
    key = project + ":" + stamp + ":" + str(stage)
    if tr.data.get("opening_notices", {}).get(key):
        return False
    reservation = tr.data.get("opening_reservation")
    if reservation:
        previous = db.get(Outbox, reservation)
        if previous and previous.status in {"pending", "uncertain", "sending"}:
            return False
    if tr.data.get("reservation"):
        previous = db.get(Outbox, tr.data["reservation"])
        if previous and previous.status in {"uncertain", "sending"}:
            curator_task(db, tr, "Перед открытием требуется сверить неопределённое напоминание.")
            return False
        cancel_stale(db, tr)
        tr.data = {**tr.data, "reservation": None}
    b = db.get(Binding, tr.binding_id)
    from ama.worker import next_partner_window

    try:
        if next_partner_window(db, b, now()) > now():
            return False
    except BusinessError:
        curator_task(db, tr, "Не настроено окно уведомлений перед внутренним открытием.")
        return False
    row = queue(
        db,
        b,
        "learning-opening:" + tr.id + ":" + key,
        "До внутреннего открытия осталось не более "
        + str(stage)
        + " дней. Обязательное ознакомление не завершено. Продолжить: /learning"
        + (" · Блокер допуска к внутреннему открытию." if stage == 1 else ""),
    )
    row.payload = {
        **row.payload,
        "partner_window": True,
        "learning_opening": {
            "track": tr.id,
            "version": tr.version,
            "project": project,
            "date": stamp,
            "stage": stage,
            "key": key,
        },
    }
    db.flush()
    tr.data = {**tr.data, "opening_reservation": row.id}
    if stage <= 3:
        curator_task(db, tr, "Контроль незавершённого ознакомления перед внутренним открытием: D−" + str(stage))
    return True


def replace_binding(db, old, new):
    for tr in db.scalars(
        select(LearningTrack).where(LearningTrack.bot_id == settings().bot_id, LearningTrack.binding_id == old.id)
    ):
        tr.binding_id = new.id
        tr.version += 1
        tr.data = {
            **tr.data,
            "reservation": None,
            "opening_reservation": None,
            "history": [
                *tr.data.get("history", []),
                {"action": "binding_alias", "old": old.id, "new": new.id, "at": now().isoformat() + "Z"},
            ],
        }


def prepare_content(db, actor):
    from ama.admin import check_admin
    from ama.learning_catalog import REQUIREMENTS

    check_admin(db, actor)
    created = []
    for item in REQUIREMENTS:
        if db.scalar(select(Record.id).where(Record.kind == "material", Record.key == item["key"])):
            continue
        r = Record(
            kind="material",
            key=item["key"],
            published=False,
            data={
                "title": item["title"],
                "module": item["module"],
                "audience": ["partner"],
                "availability": "blocked",
                "content_plan": True,
            },
        )
        db.add(r)
        created.append(item["key"])
    return {"created": created}


def material_updates(db, actor, rec, previous):
    if not previous or not rec.published or not rec.data.get("substantial_update"):
        return
    if not rec.data.get("required_update_reason") or not rec.data.get("update_cards"):
        raise BusinessError("Изменение материала требует причины и карточек изменений.")
    people = {
        t.person_id
        for t in db.scalars(select(Training))
        if any(rec.key in c.get("required_materials", []) for c in t.cards)
    }
    for person in people:
        key = "material_update:" + rec.id
        if db.scalar(
            select(Training.id).where(
                Training.person_id == person, Training.course_key == key, Training.course_version == rec.version
            )
        ):
            continue
        target = None
        for b in db.scalars(
            select(Binding).where(
                Binding.bot_id == settings().bot_id,
                Binding.purpose == "client",
                Binding.active.is_(True),
                Binding.verified.is_(True),
            )
        ):
            if active_person_role(db, person, {"partner"}, b) and can_material(
                db, Actor(person, b.id), rec, lesson=True
            ):
                target = b
                break
        if not target:
            continue
        cards = deepcopy(rec.data["update_cards"])
        for c in cards:
            c["required_materials"] = sorted(set([*c.get("required_materials", []), rec.key]))
        t = Training(person_id=person, course_key=key, course_version=rec.version, cards=cards, position=0)
        db.add(t)
        db.flush()
        tr = ensure_track(db, Actor(person, target.id), t, "update")
        tr.course_key = "general"
        row = queue(
            db,
            target,
            "learning-assignment:" + tr.id,
            "Обязательное обновление материала: "
            + rec.data["required_update_reason"]
            + "\n/learning — карточки изменений.",
        )
        row.payload = {**row.payload, "learning_assignment": tr.id}
