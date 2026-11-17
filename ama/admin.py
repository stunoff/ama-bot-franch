"""Настройки из /af_settings. bootstrap CLI вызывает тот же валидатор без чата."""
from sqlalchemy import select
from ama.auth import require
from ama.calendar import Calendar
from ama.config import settings
from ama.errors import BusinessError, AccessDenied
from ama.models import Identity, Account, Entity, EstablishmentBrand, Role, Binding, Record, Audit, Work, Outbox
from ama.service import versioned, stamp

ROLES = {"admin", "partner", "observer", "curator", "support_head", "marketing_manager", "specialist",
         "contract", "consultant", "chief_marketer"}
KINDS = {"calendar", "material", "course", "route", "commercial", "consent", "template", "policy"}


def check_admin(db, actor):
    require(db, actor, {"admin"})
    b = db.get(Binding, actor.binding_id)
    if b.purpose != "management":
        raise AccessDenied("Настройки доступны в «Управление сопровождения УК».")


def apply(db, actor, command, *, bootstrap=False):
    if not bootstrap:
        check_admin(db, actor)
    kind, d = command["type"], command["data"]
    obj = None
    old = None
    if kind == "person":
        obj = Identity(id=d.get("id"), name=d["name"])
        db.add(obj)
    elif kind == "account":
        if not db.get(Identity, d["person_id"]) or d["platform"] not in {"telegram", "max"}:
            raise BusinessError("Некорректный человек/мессенджер.")
        obj = Account(person_id=d["person_id"], platform=d["platform"], external_id=str(d["external_id"]))
        db.add(obj)
    elif kind == "entity":
        if d["kind"] not in {"partner", "brand", "establishment"}:
            raise BusinessError("Неизвестный тип сущности.")
        if d["kind"] == "establishment":
            parent = db.get(Entity, d.get("partner_id"))
            if not parent or parent.kind != "partner":
                raise BusinessError("У предприятия должен быть реальный партнёр.")
        obj = Entity(id=d.get("id"), kind=d["kind"], name=d["name"], partner_id=d.get("partner_id"), data=d.get("data", {}))
        db.add(obj)
    elif kind == "link_brand":
        est, brand = db.get(Entity, d["establishment_id"]), db.get(Entity, d["brand_id"])
        if not est or est.kind != "establishment" or not brand or brand.kind != "brand":
            raise BusinessError("Неверная связь предприятия с брендом.")
        obj = EstablishmentBrand(establishment_id=est.id, brand_id=brand.id)
        db.add(obj)
    elif kind == "role":
        if d["name"] not in ROLES:
            raise BusinessError("Неизвестная роль.")
        if not db.get(Identity, d["person_id"]):
            raise BusinessError("Человек не найден.")
        if d["name"] in {"partner", "observer"} and not d.get("partner_id"):
            raise BusinessError("Клиентская роль требует области партнёра.")
        obj = Role(bot_id=settings().bot_id, person_id=d["person_id"], name=d["name"],
                   partner_id=d.get("partner_id"), brand_id=d.get("brand_id"), establishment_id=d.get("establishment_id"))
        db.add(obj)
    elif kind == "revoke_role":
        obj = db.scalar(select(Role).where(Role.id == d["id"]).with_for_update())
        if not obj:
            raise BusinessError("Назначение не найдено.")
        if obj.name == "admin" and obj.active:
            others = list(db.scalars(select(Role).where(Role.name == "admin", Role.active.is_(True), Role.id != obj.id).with_for_update()))
            if not others:
                raise BusinessError("Нельзя отозвать последнего администратора.")
        if db.scalar(select(Work).where(Work.status != "done", (Work.owner_id == obj.person_id) | (Work.reviewer_id == obj.person_id))):
            raise BusinessError("Сначала передайте незавершённые работы/приёмку.")
        obj.active = False
    elif kind == "binding":
        if d["platform"] not in {"telegram", "max"}:
            raise BusinessError("Неизвестный мессенджер.")
        # Создаётся отключённым. Верификация — отдельная успешная тестовая доставка.
        obj = Binding(bot_id=settings().bot_id, platform=d["platform"], chat_id=str(d["chat_id"]),
                      topic_key=str(d.get("topic_key", "")), purpose=d["purpose"], partner_id=d.get("partner_id"),
                      brand_id=d.get("brand_id"), establishment_id=d.get("establishment_id"), active=False, verified=False)
        if obj.purpose == "client" and not (obj.partner_id and obj.brand_id):
            raise BusinessError("Клиентский чат требует партнёра и бренд.")
        db.add(obj)
    elif kind == "probe_binding":
        obj = db.get(Binding, d["id"])
        if not obj:
            raise BusinessError("Маршрут не найден.")
        db.add(Outbox(dedupe_key=f"probe:{obj.id}:{obj.version}:{d['nonce']}", platform=obj.platform,
                      chat_id=obj.chat_id, topic_key=obj.topic_key, binding_id=obj.id, binding_version=obj.version,
                      payload={"text": "АМА Франчайзинг: тест доставки маршрута.", "probe": True}))
    elif kind == "activate_binding":
        obj = db.scalar(select(Binding).where(Binding.id == d["id"]).with_for_update())
        if not obj or not obj.verified:
            raise BusinessError("Сначала выполните успешную тестовую доставку.")
        versioned(obj, d["version"])
        old = obj.version
        if obj.purpose == "client":
            if not db.scalar(select(Role).where(Role.name == "partner", Role.partner_id == obj.partner_id, Role.active.is_(True))):
                raise BusinessError("Не назначен ФП.")
            obj.active_client_key = f"{settings().bot_id}:{obj.partner_id}:{obj.brand_id}"
        obj.active, obj.version = True, obj.version + 1
    elif kind == "replace_binding":
        obj = db.scalar(select(Binding).where(Binding.id == d["old_id"]).with_for_update())
        new = db.scalar(select(Binding).where(Binding.id == d["new_id"]).with_for_update())
        if not obj or not new or obj.id == new.id or not new.verified:
            raise BusinessError("Нужна проверенная новая привязка.")
        versioned(obj, d["version"])
        if any(getattr(obj, k) != getattr(new, k) for k in ("purpose", "partner_id", "brand_id", "establishment_id")):
            raise BusinessError("Области старого и нового маршрута должны совпадать.")
        old = obj.version
        obj.active, obj.active_client_key, obj.version = False, None, obj.version + 1
        db.flush()
        new.active, new.version = True, new.version + 1
        if new.purpose == "client":
            new.active_client_key = f"{settings().bot_id}:{new.partner_id}:{new.brand_id}"
        for w in db.scalars(select(Work).where((Work.binding_id == obj.id) | (Work.client_binding_id == obj.id) | (Work.pending_binding_id == obj.id)).with_for_update()):
            if w.binding_id == obj.id:
                w.binding_id = new.id
            if w.client_binding_id == obj.id:
                w.client_binding_id = new.id
            if w.pending_binding_id == obj.id:
                w.pending_binding_id = new.id
            # Версии работы не меняются: бизнес-состояние и действующий срок остаются прежними.
        for event in db.scalars(select(Outbox).where(Outbox.binding_id == obj.id, Outbox.status.in_(["pending", "failed"]))):
            event.binding_id, event.binding_version = new.id, new.version
            event.platform, event.chat_id, event.topic_key = new.platform, new.chat_id, new.topic_key
    elif kind == "record":
        if d["kind"] not in KINDS:
            raise BusinessError("Неизвестный справочник.")
        obj = db.scalar(select(Record).where(Record.kind == d["kind"], Record.key == d["key"]).with_for_update())
        if obj:
            versioned(obj, d["version"])
            old = obj.version
            if obj.published and not d.get("published", False) and obj.kind in {"material", "course", "route"}:
                raise BusinessError("Архивация действующей записи требует анализа зависимостей; используйте замену версии.")
            obj.data, obj.version = d["value"], obj.version + 1
        else:
            obj = Record(kind=d["kind"], key=d["key"], data=d["value"], owner_id=d.get("owner_id"))
            db.add(obj)
        if d.get("published"):
            if not obj.owner_id and not d.get("owner_id"):
                raise BusinessError("Для публикации укажите владельца содержимого.")
            if obj.kind == "calendar":
                Calendar(obj.data)
            if obj.kind == "material":
                from urllib.parse import urlparse
                if urlparse(obj.data.get("url", "")).scheme != "https" or not obj.data.get("audience"):
                    raise BusinessError("Материал требует HTTPS-ссылку и аудиторию.")
            if obj.kind == "consent" and not all(obj.data.get(k) for k in ("text", "privacy_url")):
                raise BusinessError("Требуется утверждённый текст согласия и ссылка на политику.")
            if obj.kind == "course" and not obj.data.get("cards"):
                raise BusinessError("Нет карточек ознакомления.")
            if obj.kind == "route":
                from ama.auth import active_person_role
                from types import SimpleNamespace
                scope = SimpleNamespace(partner_id=obj.data.get("partner_id"), brand_id=obj.data.get("brand_id"), establishment_id=None)
                b = db.get(Binding, obj.data.get("binding_id"))
                if not b or not b.active or not b.verified or not obj.data.get("calendar"):
                    raise BusinessError("Маршрут требует проверенный чат и календарь.")
                for k in ("owner_id", "reviewer_id"):
                    if not active_person_role(db, obj.data.get(k), ROLES - {"admin", "observer"}, scope):
                        raise BusinessError("Не назначен исполнитель/принимающий маршрута.")
                from ama.service import record
                record(db, "calendar", obj.data["calendar"])
        obj.published = d.get("published", False)
        obj.owner_id = d.get("owner_id", obj.owner_id)
    elif kind == "entity_update":
        obj = db.scalar(select(Entity).where(Entity.id == d["id"]).with_for_update())
        if not obj:
            raise BusinessError("Сущность не найдена.")
        versioned(obj, d["version"])
        old = obj.version
        obj.data, obj.version = {**obj.data, **d["value"]}, obj.version + 1
    else:
        raise BusinessError("Неизвестная настройка.")
    db.flush()
    if actor:
        stamp(db, actor, obj, f"CONFIG_{kind.upper()}", old, role="admin")
    else:
        db.add(Audit(object_id=obj.id, action=f"BOOTSTRAP_{kind.upper()}", data={}))
    return {"id": obj.id, "version": getattr(obj, "version", None)}


def inspect(db, actor):
    check_admin(db, actor)
    return {"entities": [{"id": x.id, "kind": x.kind, "name": x.name, "version": x.version} for x in db.scalars(select(Entity))],
            "people": [{"id": x.id, "name": x.name} for x in db.scalars(select(Identity))],
            "bindings": [{"id": x.id, "purpose": x.purpose, "platform": x.platform, "chat_id": x.chat_id,
                          "active": x.active, "verified": x.verified, "version": x.version} for x in db.scalars(select(Binding))],
            "configuration": [{"kind": x.kind, "key": x.key, "published": x.published, "version": x.version} for x in db.scalars(select(Record))],
            "delivery_errors": [{"id": x.id, "status": x.status, "attempts": x.attempts} for x in db.scalars(select(Outbox).where(Outbox.status.in_(["failed", "uncertain"])))]}
