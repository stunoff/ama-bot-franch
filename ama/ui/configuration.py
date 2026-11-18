"""Versioned configuration operations used by the chat forms and protected CLI."""
from copy import deepcopy
from datetime import datetime
from urllib.parse import urlsplit
from string import Formatter
from sqlalchemy import select, or_
from ama.auth import active_person_role
from ama.config import settings
from ama.errors import BusinessError, AccessDenied
from ama.models import (Identity, Account, Role, Entity, EstablishmentBrand, Binding, Record, Work, Training,
                        ConfigurationRevision, Timer, Outbox, UIDraft, Inbox, Attachment, Audit, now)
from ama.service import versioned, parse_date, stamp, queue

MODELS = {"person": Identity, "account": Account, "role": Role, "entity": Entity, "binding": Binding,
          "record": Record, "link": EstablishmentBrand}
SECRET_KEYS = {"token", "telegram_token", "max_token", "password", "secret", "api_key", "private_key", "webhook_secret"}


def snapshot(obj):
    if obj is None:
        return {}
    result = {}
    for col in obj.__table__.columns:
        value = getattr(obj, col.name)
        result[col.name] = value.isoformat() if isinstance(value, datetime) else deepcopy(value)
    return result


def safe_values(value):
    if isinstance(value, dict):
        if SECRET_KEYS & {str(k).lower() for k in value}:
            raise BusinessError("Секреты и пароли настраиваются на сервере, а не в чате.")
        for child in value.values():
            safe_values(child)
    elif isinstance(value, list):
        for child in value:
            safe_values(child)


def get_person(db, person_id):
    p = db.get(Identity, person_id) if person_id else None
    if not p:
        raise BusinessError("Человек не найден. Сначала добавьте сотрудника.")
    return p


def validate_scope(db, data):
    for key, kind in (("partner_id", "partner"), ("brand_id", "brand"), ("establishment_id", "establishment")):
        if data.get(key):
            obj = db.get(Entity, data[key])
            if not obj or obj.kind != kind or obj.data.get("archived"):
                raise BusinessError("Область назначения содержит отсутствующую или архивную запись.")
    if data.get("establishment_id"):
        e = db.get(Entity, data["establishment_id"])
        if data.get("partner_id") and e.partner_id != data["partner_id"]:
            raise BusinessError("Предприятие не принадлежит выбранному ФП.")
        if data.get("brand_id") and not db.scalar(select(EstablishmentBrand).where(
            EstablishmentBrand.establishment_id == e.id, EstablishmentBrand.brand_id == data["brand_id"], EstablishmentBrand.active.is_(True))):
            raise BusinessError("Бренд не подключён к выбранной площадке.")


def validate_role(db, data):
    get_person(db, data.get("person_id"))
    validate_scope(db, data)
    from ama.admin import ROLES
    if data.get("name") not in ROLES:
        raise BusinessError("Неизвестная роль.")
    if data["name"] in {"partner", "observer"} and not data.get("partner_id"):
        raise BusinessError("Для клиентской роли выберите ФП.")
    if data["name"] == "admin" and any(data.get(k) for k in ("partner_id", "brand_id", "establishment_id")):
        raise BusinessError("Системный администратор назначается в техническом контуре без бизнес-области.")
    if data.get("expires_at") and parse_date(data["expires_at"]) <= now():
        raise BusinessError("Срок назначения должен быть в будущем.")


def guard_last_admin(db, *, role_id=None, account_id=None):
    roles = list(db.scalars(select(Role).where(Role.bot_id == settings().bot_id, Role.name == "admin", Role.active.is_(True)).with_for_update()))
    for r in roles:
        if r.id == role_id or r.expires_at and r.expires_at <= now():
            continue
        if any((r.partner_id, r.brand_id, r.establishment_id)):
            continue
        if db.scalar(select(Account).where(Account.person_id == r.person_id, Account.active.is_(True),
                                           Account.id != account_id if account_id else True)):
            return
    raise BusinessError("Нельзя отключить последнего доступного системного администратора. Сначала проверьте замену.")


def guard_person_work(db, person_id):
    if db.scalar(select(Work).where(Work.status != "done", or_(Work.owner_id == person_id, Work.reviewer_id == person_id,
                                                               Work.pending_owner_id == person_id))):
        raise BusinessError("Есть активные работы или приёмка. Сначала выполните явную передачу / замещение.")


def contains(value, needle):
    if isinstance(value, dict):
        return any(contains(child, needle) for child in value.values())
    if isinstance(value, list):
        return any(contains(child, needle) for child in value)
    return value == needle


def dependencies(db, rec):
    rows = []
    for other in db.scalars(select(Record).where(Record.id != rec.id, Record.published.is_(True))):
        if contains(other.data, rec.key) or contains(other.data, rec.id):
            rows.append(f"{other.kind}: {other.data.get('title', other.key)}")
    for w in db.scalars(select(Work).where(Work.status != "done")):
        if contains(w.data, rec.key) or contains(w.data, rec.id) or rec.kind == "route" and w.kind == rec.key:
            rows.append(f"Работа: {w.title}")
    if rec.kind == "material":
        for t in db.scalars(select(Training).where(Training.completed_at.is_(None))):
            if contains(t.cards, rec.key):
                rows.append("Незавершённое персональное ознакомление")
    return sorted(set(rows))


def validate_record(db, kind, value, *, published=False):
    safe_values(value)
    if not isinstance(value, dict):
        raise BusinessError("Содержимое записи должно состоять из полей.")
    if kind in {"commercial", "consent", "material", "route"}:
        validate_scope(db, value)
    for field in ("url", "privacy_url", "reference"):
        if value.get(field):
            parsed = urlsplit(value[field])
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise BusinessError("Используйте HTTPS-ссылку без логина, пароля и секретов доступа.")
    if kind == "policy" and value.get("policy_type") == "sales_content":
        validate_scope(db, value)
        if published and not all(value.get(k) for k in ("title", "text", "brand_id", "approval", "approved_on")):
            raise BusinessError("Карточка продаж требует бренд, текст, владельца, дату и основание утверждения.")
        if value.get("category") == "finance" and value.get("finance_basis") not in {"terms", "model"}:
            raise BusinessError("Раздел экономики должен отделять условия франшизы от финансовой модели.")
        if value.get("category") == "finance" and value.get("finance_basis") == "model" and not all(value.get(k) for k in ("format", "assumptions")):
            raise BusinessError("Финансовая модель требует формат, дату и допущения расчёта.")
        for link in value.get("links", []):
            parsed = urlsplit(link.get("url", ""))
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise BusinessError("Материал требует безопасную HTTPS-ссылку.")
    if kind == "calendar":
        from ama.calendar import Calendar
        Calendar(value)
        for date in value.get("holidays", []) + value.get("extra_workdays", []):
            datetime.strptime(date, "%Y-%m-%d")
    if kind == "course":
        cards = value.get("cards", [])
        if not cards and published:
            raise BusinessError("Для публикации добавьте карточки ознакомления.")
        for c in cards:
            if not c.get("title") or not c.get("text") or not isinstance(c.get("module", 0), int) or not 0 <= c.get("module", 0) <= 11:
                raise BusinessError("Карточка требует название, текст и раздел 0–11.")
            for key in c.get("required_materials", []):
                material = db.scalar(select(Record).where(Record.kind == "material", Record.key == key))
                if not material or published and not material.published:
                    raise BusinessError(f"Обязательный материал {key} не опубликован.")
    if kind == "template":
        permitted = {"partner_name", "brand", "brand_name", "establishment_name", "request_id", "title", "status", "due_at", "owner_name"}
        for _, name, fmt, conversion in Formatter().parse(value.get("text", "")):
            if name is not None and (name not in permitted or fmt or conversion):
                raise BusinessError("В шаблоне есть неподдерживаемая переменная или форматирование.")
    if kind == "commercial" and published:
        if not value.get("approval") or not value.get("approved_on") or not value.get("terms"):
            raise BusinessError("Публикация коммерческих условий требует текста, даты и основания владельца содержимого.")
        if not isinstance(value.get("required_minor"), int) or value["required_minor"] < 0:
            raise BusinessError("Укажите утверждённое условие расчёта.")
    if kind == "policy" and value.get("policy_type") == "scenario":
        blocks = value.get("blocks", [])
        keys = [b.get("key") for b in blocks]
        if not keys or not all(keys) or len(keys) != len(set(keys)):
            raise BusinessError("Пункты сценария требуют уникальные коды.")
        graph = {b["key"]: b.get("depends_on", []) for b in blocks}
        visited, visiting = set(), set()
        def visit(key):
            if key not in graph:
                raise BusinessError("Зависимость ссылается на отсутствующий пункт.")
            if key in visiting:
                raise BusinessError("В зависимостях сценария найден цикл.")
            if key in visited:
                return
            visiting.add(key)
            for dep in graph[key]:
                visit(dep)
            visiting.remove(key)
            visited.add(key)
        for key in graph:
            visit(key)
        for b in blocks:
            for field in ("expected_result", "executor_role", "reviewer_role", "route", "start_event", "completion_rule"):
                if not b.get(field):
                    raise BusinessError("У каждого пункта нужны результат, роли, маршрут, событие и правило приёмки.")
            if published:
                route = db.scalar(select(Record).where(Record.kind == "route", Record.key == b["route"], Record.published.is_(True)))
                if not route:
                    raise BusinessError("Маршрут пункта не опубликован.")
                for key in b.get("materials", []):
                    if not db.scalar(select(Record).where(Record.kind == "material", Record.key == key, Record.published.is_(True))):
                        raise BusinessError("Материал пункта не опубликован.")
    if kind == "policy" and published and value.get("policy_type") in {"scenario", "sla", "payment", "schedule", "faq"}:
        if not value.get("approval"):
            raise BusinessError("Укажите основание уполномоченного владельца процесса.")


def revision(db, actor, kind, obj, before, reason):
    after = snapshot(obj)
    db.add(ConfigurationRevision(object_id=obj.id, kind=kind, actor_id=actor.person_id if actor else None,
                                  old_version=before.get("version"), new_version=after.get("version"), before=before,
                                  after=after, reason=reason or "Защищённая первоначальная настройка"))


def bump_timers(db, w):
    for t in db.scalars(select(Timer).where(Timer.work_id == w.id, Timer.status == "pending")):
        t.data = {**t.data, "version": w.version}


def reviewer_roles(work):
    from ama.service import STAFF
    if work.kind.startswith("marketing"):
        return {"marketing_manager"}
    if work.kind in {"legal", "debt"}:
        return {"contract"}
    if work.kind in {"internal_opening", "technical_opening", "official_opening"}:
        return {"curator"}
    return STAFF


def substitute(db, actor, d):
    source, target = get_person(db, d["from_person"]), get_person(db, d["to_person"])
    if source.id == target.id or not d.get("reason") or not d.get("work_ids"):
        raise BusinessError("Выберите разных сотрудников, работы и основание передачи.")
    due = parse_date(d["expires_at"]) if d.get("expires_at") else None
    if due and due <= now():
        raise BusinessError("Окончание замещения должно быть в будущем.")
    plan = []
    for work_id in sorted(set(d["work_ids"])):
        w = db.scalar(select(Work).where(Work.id == work_id).with_for_update())
        if not w or w.status == "done" or w.pending_owner_id:
            raise BusinessError("Выбранная работа завершена, отсутствует или имеет неподтверждённую передачу.")
        fields = [k for k in ("owner_id", "executor_id", "reviewer_id") if getattr(w, k) == source.id]
        if not fields:
            raise BusinessError("Исходный сотрудник не назначен на выбранную работу.")
        old = snapshot(w)
        from ama.service import STAFF
        for field in fields:
            allowed = reviewer_roles(w) if field == "reviewer_id" else STAFF
            if not active_person_role(db, target.id, allowed, w):
                raise BusinessError("Новый сотрудник не имеет подходящей действующей роли для каждой выбранной работы.")
        for field in fields:
            setattr(w, field, target.id)
        w.version += 1
        bump_timers(db, w)
        stamp(db, actor, w, "STAFF_SUBSTITUTED", old["version"], {"from": source.id, "to": target.id, "reason": d["reason"]}, "admin")
        queue(db, db.get(Binding, w.binding_id), f"substitution:{w.id}:{w.version}", f"Ответственный по работе «{w.title}» изменён: {source.name} → {target.name}. Срок сохранён.", work=w)
        plan.append({"work_id": w.id, "fields": fields, "from": source.id, "to": target.id})
    return {"id": source.id, "version": source.version, "plan": plan, "count": len(plan)}


def extra(db, actor, kind, d):
    """Return None for commands delegated to the original admin.apply implementation."""
    if kind == "reconcile_delivery":
        obj = db.scalar(select(Outbox).where(Outbox.id == d["id"]).with_for_update())
        if not obj or obj.status != d["expected_status"] or obj.status not in {"sending", "uncertain"}:
            raise BusinessError("Состояние доставки изменилось. Обновите диагностику.")
        if not d.get("evidence") or d.get("outcome") not in {"delivered", "not_delivered"}:
            raise BusinessError("Нужен проверенный исход и описание сверки.")
        if obj.status == "sending" and not d.get("worker_stopped"):
            raise BusinessError("Перед сверкой sending остановите все воркеры, дождитесь завершения отправок и подтвердите это в форме. Обрабатывать форму можно после запуска одного воркера.")
        if d["outcome"] == "delivered":
            if not d.get("external_id"):
                raise BusinessError("Укажите фактический ID доставленного сообщения.")
            obj.status, obj.external_id, obj.error = "sent", d["external_id"], None
            from ama import reminders
            reminders.on_delivered(db, obj)
            if obj.payload.get("op") == "create_topic":
                from ama.sale_flow import on_topic
                on_topic(db, obj, d["external_id"])
            if obj.payload.get("probe"):
                b = db.get(Binding, obj.binding_id)
                if b and b.version == obj.binding_version:
                    b.verified = True
        else:
            obj.status, obj.error, obj.attempts, obj.retry_at = "pending", None, 0, now()
        db.add(Audit(actor_id=actor.person_id, object_id=obj.id, action="CONFIG_DELIVERY_RECONCILED", data={"reason": d["reason"], "evidence": d["evidence"], "outcome": d["outcome"]}))
        return {"id": obj.id}
    if kind in {"retry_delivery", "retry_inbox", "retry_attachment", "retry_timer"}:
        model = {"retry_delivery": Outbox, "retry_inbox": Inbox, "retry_attachment": Attachment, "retry_timer": Timer}[kind]
        pk = model.key if model is Inbox else model.id
        obj = db.scalar(select(model).where(pk == d["id"]).with_for_update())
        if not obj or obj.status != d["expected_status"]:
            raise BusinessError("Состояние очереди изменилось. Обновите диагностику.")
        allowed = {"retry_delivery": {"failed", "blocked"}, "retry_inbox": {"failed", "rejected"},
                   "retry_attachment": {"failed"}, "retry_timer": {"blocked"}}[kind]
        if obj.status not in allowed:
            raise BusinessError("Повтор недопустим без сверки фактического исхода.")
        if isinstance(obj, Outbox) and obj.external_id:
            raise BusinessError("Есть идентификатор доставки. Сначала выполните сверку.")
        obj.status = "pending"
        if hasattr(obj, "attempts"):
            obj.attempts = 0
        if hasattr(obj, "retry_at"):
            obj.retry_at = now()
        if hasattr(obj, "error"):
            obj.error = None
        db.add(Audit(actor_id=actor.person_id, object_id=d["id"], action="CONFIG_"+kind.upper(), data={"reason": d["reason"]}))
        return {"id": d["id"]}
    if kind == "employee":
        from ama import admin
        person_id = d.get("person_id")
        if not person_id:
            person_id = admin.apply(db, actor, {"type": "person", "data": {"name": d["name"], "reason": d["reason"]}}, bootstrap=actor is None)["id"]
        admin.apply(db, actor, {"type": "account", "data": {"person_id": person_id, "platform": d["platform"], "external_id": d["external_id"], "reason": d["reason"]}}, bootstrap=actor is None)
        result = admin.apply(db, actor, {"type": "role", "data": {**d, "person_id": person_id, "name": d["role"]}}, bootstrap=actor is None)
        return {**result, "person_id": person_id}
    if kind == "substitution":
        return substitute(db, actor, d)
    if kind in {"person_update", "account_update", "role_update", "binding_update", "link_update", "archive_record", "disable_binding"}:
        model_kind = kind.split("_")[0]
        if kind == "archive_record":
            model_kind = "record"
        if kind == "disable_binding":
            model_kind = "binding"
        obj = db.scalar(select(MODELS[model_kind]).where(MODELS[model_kind].id == d["id"]).with_for_update())
        if not obj:
            raise BusinessError("Запись не найдена.")
        versioned(obj, d["version"])
        if hasattr(obj, "bot_id") and obj.bot_id != settings().bot_id:
            raise AccessDenied("Запись относится к другому боту.")
        before = snapshot(obj)
        if kind == "person_update":
            if not d.get("name") or len(d["name"]) > 200:
                raise BusinessError("Имя должно содержать 1–200 символов.")
            obj.name = d["name"]
        elif kind == "account_update":
            if any(k in d and str(d[k]) != str(getattr(obj, k)) for k in ("person_id", "platform", "external_id")):
                raise BusinessError("Аккаунт нельзя перепривязать другому человеку. Добавьте новый подтверждённый аккаунт.")
            if not d.get("active", True):
                if db.scalar(select(Role).where(Role.person_id == obj.person_id, Role.name == "admin", Role.bot_id == settings().bot_id, Role.active.is_(True))):
                    guard_last_admin(db, account_id=obj.id)
                guard_person_work(db, obj.person_id)
            obj.active = d.get("active", True)
        elif kind == "role_update":
            validate_role(db, d)
            if obj.person_id != d["person_id"]:
                raise BusinessError("Назначение другого человека создаётся отдельно.")
            guard_person_work(db, obj.person_id)
            if obj.name == "admin":
                guard_last_admin(db, role_id=obj.id)
            for key in ("name", "partner_id", "brand_id", "establishment_id"):
                setattr(obj, key, d.get(key))
            obj.expires_at = parse_date(d["expires_at"]) if d.get("expires_at") else None
        elif kind == "binding_update":
            if obj.active:
                raise BusinessError("Активный маршрут меняется через проверенную замену чата.")
            if any(k in d and d[k] != getattr(obj, k) for k in ("partner_id", "brand_id", "establishment_id", "purpose")):
                raise BusinessError("Для другой области или назначения создайте новую привязку.")
            if d.get("platform", obj.platform) not in {"telegram", "max"}:
                raise BusinessError("Неизвестный мессенджер.")
            for key in ("platform", "chat_id", "topic_key"):
                if key in d:
                    setattr(obj, key, str(d[key] or ""))
            if obj.platform == "max" and obj.topic_key:
                raise BusinessError("MAX не использует Telegram topic_id.")
            obj.verified = False
        elif kind == "link_update":
            if not d.get("active", True):
                if db.scalar(select(Work).where(Work.establishment_id == obj.establishment_id, Work.brand_id == obj.brand_id, Work.status != "done")):
                    raise BusinessError("Связь имеет активные работы: сначала обработайте их явно.")
            obj.active = d.get("active", True)
        elif kind == "archive_record":
            deps = dependencies(db, obj)
            if deps:
                raise BusinessError("Архивация заблокирована зависимостями: " + "; ".join(deps[:8]))
            obj.published = False
        elif kind == "disable_binding":
            if obj.purpose == "management" and not db.scalar(select(Binding).where(
                Binding.bot_id == settings().bot_id, Binding.id != obj.id, Binding.purpose == "management", Binding.active.is_(True), Binding.verified.is_(True))):
                raise BusinessError("Основной управляющий маршрут нельзя отключить без проверенной замены.")
            if db.scalar(select(Work).where(Work.status != "done", or_(Work.binding_id == obj.id, Work.client_binding_id == obj.id, Work.pending_binding_id == obj.id))):
                raise BusinessError("Есть открытые работы. Используйте перенос привязки.")
            obj.active, obj.active_client_key = False, None
        obj.version += 1
        db.flush()
        revision(db, actor, model_kind, obj, before, d.get("reason"))
        if actor:
            stamp(db, actor, obj, "CONFIG_" + kind.upper(), before["version"], role="admin")
        return {"id": obj.id, "version": obj.version}
    return None


def complete_substitution(db, draft):
    """Return temporary assignments safely; never leave work without a valid owner."""
    from ama.service import STAFF
    attention = []
    for entry in draft.data.get("applied_plan", []):
        w = db.scalar(select(Work).where(Work.id == entry["work_id"]).with_for_update())
        if not w or w.status == "done":
            continue
        fields = [k for k in entry["fields"] if getattr(w, k) == entry["to"]]
        if not fields:
            continue
        if any(not active_person_role(db, entry["from"], reviewer_roles(w) if key == "reviewer_id" else STAFF, w) for key in fields):
            attention.append(w.id)
            continue
        old = w.version
        for key in fields:
            setattr(w, key, entry["from"])
        w.version += 1
        bump_timers(db, w)
        stamp(db, None, w, "SUBSTITUTION_RETURNED", old, {"draft_id": draft.id})
        queue(db, db.get(Binding, w.binding_id), f"substitution-return:{draft.id}:{w.id}", f"Замещение по работе «{w.title}» завершено; прежние сроки сохранены.", work=w)
    if attention:
        for b in db.scalars(select(Binding).where(Binding.bot_id == settings().bot_id, Binding.purpose == "management", Binding.active.is_(True), Binding.verified.is_(True))):
            queue(db, b, f"substitution-attention:{draft.id}:{b.id}", "Нужна подтверждённая замена ответственного после окончания замещения. Текущий владелец сохранён. Работ: " + str(len(attention)))
    draft.status = "attention" if attention else "returned"


def substitution_step(db):
    for draft in db.scalars(select(UIDraft).where(UIDraft.kind == "substitution", UIDraft.status == "applied").with_for_update(skip_locked=True)):
        if draft.data.get("expires_at") and parse_date(draft.data["expires_at"]) <= now():
            complete_substitution(db, draft)
            return True
    return False
