"""Операции вызываются внутри одной транзакции; Work читается FOR UPDATE."""
from datetime import datetime
from sqlalchemy import select
from ama.auth import require, roles_for, active_person_role
from ama.calendar import Calendar
from ama.errors import BusinessError, Conflict, AccessDenied
from ama.models import (Work, Binding, Entity, EstablishmentBrand, Audit, Receipt, Outbox, Timer,
                        Attachment, Record, Training, now)

STAFF = {"curator", "support_head", "marketing_manager", "specialist", "contract", "chief_marketer", "consultant"}
STATUSES = {"new": "Новое / в очереди", "in_progress": "В работе", "waiting_partner": "Нужны данные ФП",
            "waiting_uk": "Нужно действие УК", "waiting_external": "Ожидаем внешнюю сторону",
            "materials": "Материалы поступают", "review": "На проверке", "rework": "Нужна доработка", "done": "Завершено"}


def stamp(db, actor, obj, action, old=None, data=None, role=None):
    db.add(Audit(actor_id=actor.person_id if actor else None, role=role, object_id=obj.id,
                 action=action, old_version=old, new_version=getattr(obj, "version", None), data=data or {}))


def versioned(obj, version):
    if obj.version != version:
        raise Conflict(f"Карточка изменена. Откройте её снова (актуальная версия {obj.version}).")


def record(db, kind, key):
    result = db.scalar(select(Record).where(Record.kind == kind, Record.key == key, Record.published.is_(True)))
    if not result:
        raise BusinessError(f"Не опубликована настройка {kind}/{key}.")
    return result


def queue(db, binding, key, text, *, work=None, buttons=None, attachment_id=None):
    if not binding or not binding.active or not binding.verified:
        raise BusinessError("Маршрут не активирован или не проверен.")
    existing = db.scalar(select(Outbox).where(Outbox.dedupe_key == key))
    if existing:
        return existing
    row = Outbox(dedupe_key=key, binding_id=binding.id, binding_version=binding.version,
                 platform=binding.platform, chat_id=binding.chat_id, topic_key=binding.topic_key,
                 work_id=work.id if work else None,
                 payload={"text": text, "buttons": buttons or [], "attachment_id": attachment_id})
    db.add(row)
    return row


def make_timer(db, work, kind, due, key=None, data=None):
    key = key or f"{work.id}:{kind}:v{work.version}"
    if not db.scalar(select(Timer).where(Timer.key == key)):
        db.add(Timer(key=key, work_id=work.id, due_at=due, original_due_at=due, kind=kind,
                     data={"version": work.version, **(data or {})}))


def validate_scope(db, partner_id, brand_id=None, establishment_id=None):
    partner = db.get(Entity, partner_id)
    if not partner or partner.kind != "partner":
        raise BusinessError("Партнёр не найден.")
    if brand_id:
        brand = db.get(Entity, brand_id)
        if not brand or brand.kind != "brand":
            raise BusinessError("Бренд не найден.")
    if establishment_id:
        est = db.get(Entity, establishment_id)
        if not est or est.kind != "establishment" or est.partner_id != partner_id:
            raise BusinessError("Предприятие не принадлежит партнёру.")
        if brand_id and not db.scalar(select(EstablishmentBrand).where(
            EstablishmentBrand.establishment_id == establishment_id,
            EstablishmentBrand.brand_id == brand_id, EstablishmentBrand.active.is_(True))):
            raise BusinessError("Бренд не связан с предприятием.")


def create_work(db, actor, data, key):
    cached = db.get(Receipt, key)
    if cached:
        existing = db.get(Work, cached.response["id"])
        if not roles_for(db, actor, existing):
            raise AccessDenied("Нет доступа к исходной работе.")
        return existing
    binding = db.get(Binding, actor.binding_id)
    role = require(db, actor, {"partner", "curator", "support_head", "marketing_manager"})
    if role == "partner":
        data = {**data, "partner_id": binding.partner_id, "brand_id": binding.brand_id}
        route = record(db, "route", data.get("kind", "support")).data
        # Клиент не выбирает себе исполнителя или проверяющего.
        data = {**data, "owner_id": route["owner_id"], "reviewer_id": route["reviewer_id"],
                "binding_id": route["binding_id"], "client_binding_id": binding.id,
                "partner_person_id": actor.person_id, "calendar": route["calendar"]}
    reserved = {"sla", "sla_case", "partner_request", "paid_started_at", "offer_accepted", "opening_permission", "components", "ledger", "balance_minor", "overdue"}
    if reserved.intersection(data.get("data", {})):
        raise BusinessError("Подтверждения создаются профильной командой, а не начальными полями работы.")
    validate_scope(db, data["partner_id"], data.get("brand_id"), data.get("establishment_id"))
    due = data.get("due_at")
    if isinstance(due, str):
        due = parse_date(due)
    if not due:
        cal = Calendar(record(db, "calendar", data["calendar"]).data)
        due = cal.end_of_workday(now(), 2)
    if due <= now():
        raise BusinessError("Контрольная дата должна быть в будущем.")
    target = db.get(Binding, data["binding_id"])
    client = db.get(Binding, data.get("client_binding_id")) if data.get("client_binding_id") else None
    if not target or target.purpose == "client" or not target.active or not target.verified:
        raise BusinessError("Не заполнен внутренний маршрут работы.")
    for b in (target, client):
        if b and any(getattr(b, k) and getattr(b, k) != data.get(k)
                     for k in ("partner_id", "brand_id", "establishment_id")):
            raise AccessDenied("Маршрут не соответствует области работы.")
    work = Work(kind=data.get("kind", "support"), title=data["title"], partner_id=data["partner_id"],
                brand_id=data.get("brand_id"), establishment_id=data.get("establishment_id"),
                owner_id=data["owner_id"], executor_id=data["owner_id"], reviewer_id=data["reviewer_id"],
                binding_id=target.id, client_binding_id=client.id if client else None,
                partner_person_id=data.get("partner_person_id"), due_at=due, original_due_at=due,
                data={**data.get("data", {}), "partner_request": role == "partner", "calendar": data.get("calendar", "support")}, priority=data.get("priority", "normal"),
                parent_id=data.get("parent_id"), project_id=data.get("project_id"), origin_key=data.get("origin_key"))
    if not active_person_role(db, work.owner_id, STAFF | {"partner"}, work):
        raise BusinessError("Исполнитель не имеет действующей роли в области работы.")
    if not active_person_role(db, work.reviewer_id, STAFF | {"partner"}, work):
        raise BusinessError("Принимающий не назначен в области работы.")
    if role == "partner" and actor.person_id != work.partner_person_id:
        raise AccessDenied("Подтвердить запрос может назначенный ФП.")
    # Scoped staff may not create work for another partner/brand through unscoped queue.
    if role != "partner":
        from types import SimpleNamespace
        scope = SimpleNamespace(binding_id=actor.binding_id, client_binding_id=work.client_binding_id,
                                partner_id=work.partner_id, brand_id=work.brand_id, establishment_id=work.establishment_id)
        require(db, actor, {"curator", "support_head", "marketing_manager"}, scope)
    db.add(work)
    db.flush()
    from ama.sla import initialize
    initialize(db, work, explicit_due=bool(data.get("due_at")))
    stamp(db, actor, work, "WORK_CREATED", role=role)
    make_timer(db, work, "deadline", due)
    queue(db, target, f"created:{work.id}", f"Новая работа: {work.title}\nID: {work.id}", work=work,
          buttons=[["Открыть", f"af:w:{work.id}"]])
    db.add(Receipt(key=key, response={"id": work.id}))
    return work


def parse_date(value):
    from datetime import timezone
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, AttributeError) as e:
        raise BusinessError("Дата нужна в ISO 8601 с часовым поясом, например 2026-12-01T15:00:00+03:00.") from e


def transition(db, actor, work_id, version, action, data, key):
    w = db.scalar(select(Work).where(Work.id == work_id).with_for_update())
    if not w:
        raise BusinessError("Работа не найдена.")
    roles = roles_for(db, actor, w)  # Даже повторная команда проходит актуальную проверку доступа.
    from ama.digital import before_transition as digital_before
    digital_before(db,actor,w,action,data)
    previous = db.get(Receipt, key)
    if previous:
        return w
    versioned(w, version)
    if w.status == "done":
        raise BusinessError("Работа завершена.")
    from ama.packets import before_transition as packet_before
    packet_before(db,actor,w,action,data)
    from ama.project_flow import before_transition
    before_transition(db, actor, w, action, data)
    from ama.sla import before_transition as sla_before
    sla_before(db,w,action)
    old = w.version
    role = None
    if action == "take":
        role = require(db, actor, STAFF | {"partner"}, w)
        if actor.person_id != w.owner_id or w.status not in {"new", "waiting_uk", "rework"}:
            raise AccessDenied("Взять работу может её текущий исполнитель.")
        w.status = "in_progress"
    elif action == "submit":
        role = require(db, actor, STAFF | {"partner"}, w)
        if actor.person_id != w.owner_id or w.status not in {"in_progress", "materials", "rework"}:
            raise AccessDenied("Результат передаёт назначенный исполнитель.")
        if "partner" in roles and not (roles & STAFF) and actor.person_id != w.partner_person_id:
            raise AccessDenied("Обязательство завершает только назначенный ФП.")
        if w.kind == "digital":
            required = w.data.get("required_components", [])
            if not required or any(not w.data.get("components", {}).get(k, {}).get("confirmed") for k in required):
                raise BusinessError("Не подтверждены все применимые цифровые компоненты, включая Службу заботы.")
        if w.kind == "paid_work" and not w.data.get("paid_started_at"):
            raise BusinessError("Не согласован старт платной работы.")
        if w.kind in {"internal_opening", "technical_opening", "official_opening"} and not w.data.get("opening_permission"):
            raise BusinessError("Не подтверждён допуск к открытию.")
        if w.pending_owner_id:
            raise BusinessError("Сначала завершите передачу ответственности.")
        if db.scalar(select(Attachment).where(Attachment.work_id == w.id, Attachment.status != "stored")):
            raise BusinessError("Не все вложения сохранены. Дождитесь сохранения или устраните ошибку.")
        for dep in w.data.get("depends_on", []):
            prerequisite = db.get(Work, dep)
            if not prerequisite or prerequisite.status != "done" or prerequisite.outcome != "accepted":
                raise BusinessError("Есть непринятая зависимость.")
        if w.data.get("blockers"):
            raise BusinessError("Сначала устраните блокирующие замечания.")
        if data.get("result_description"):
            w.data = {**w.data, "result_description": data["result_description"]}
        w.status, w.owner_id = "review", w.reviewer_id
        cal = Calendar(record(db, "calendar", w.data.get("calendar", "support")).data)
        w.due_at = cal.end_of_workday(now())
    elif action in {"accept", "reject"}:
        required = {"marketing_manager"} if w.kind.startswith("marketing") else {"contract"} if w.kind in {"legal", "debt"} else {"curator"} if w.kind in {"internal_opening", "technical_opening", "official_opening"} else STAFF | {"partner"}
        role = require(db, actor, required, w)
        if actor.person_id != w.reviewer_id or w.status != "review":
            raise AccessDenied("Принять или вернуть может только назначенный принимающий.")
        if "partner" in roles and not (roles & STAFF) and actor.person_id != w.partner_person_id:
            raise AccessDenied("Подтверждение решения доступно назначенному ФП.")
        if action == "accept":
            if w.data.get("blockers"):
                raise BusinessError("Открыты блокирующие замечания.")
            for dep in w.data.get("depends_on", []):
                child = db.get(Work, dep)
                if not child or child.outcome != "accepted":
                    raise BusinessError("Зависимый результат не принят.")
            if w.kind == "support" and w.data.get("partner_request") and w.partner_person_id and actor.person_id != w.partner_person_id:
                w.data = {**w.data, "technical_reviewer_id": w.reviewer_id, "technical_result_accepted": True}
                w.status, w.owner_id, w.reviewer_id = "review", w.partner_person_id, w.partner_person_id
            else:
                w.status, w.outcome = "done", "partner_confirmed" if role == "partner" else "accepted"
        else:
            if not data.get("reason") or not data.get("due_at"):
                raise BusinessError("Для возврата нужны замечание и новый срок.")
            w.due_at = parse_date(data["due_at"])
            if w.due_at <= now():
                raise BusinessError("Новый срок должен быть в будущем.")
            w.status, w.owner_id = "rework", w.executor_id
            if actor.person_id == w.partner_person_id and w.data.get("technical_reviewer_id"):
                w.reviewer_id = w.data["technical_reviewer_id"]
            w.returns += 1
            w.data = {**w.data, "rejection": data["reason"]}
    elif action in {"wait_partner", "wait_external", "wait_uk"}:
        role = require(db, actor, STAFF, w)
        if actor.person_id != w.owner_id:
            raise AccessDenied("Ожидание устанавливает текущий ответственный.")
        if not all(data.get(k) for k in ("party", "required", "next_check")):
            raise BusinessError("Укажите сторону, ожидаемое действие и дату следующей проверки.")
        from ama.sla import case_for
        if case_for(db,w) and action=="wait_partner" and not data.get("reason"):
            raise BusinessError("Укажите, почему конкретное действие невозможно продолжить без данных ФП.")
        if case_for(db,w) and action=="wait_external" and not data.get("sent_information"):
            raise BusinessError("Зафиксируйте, какие сведения уже отправлены внешней стороне.")
        w.status = {"wait_partner": "waiting_partner", "wait_external": "waiting_external", "wait_uk": "waiting_uk"}[action]
        w.due_at = parse_date(data["next_check"])
        if w.due_at <= now():
            raise BusinessError("Проверка должна быть назначена в будущем.")
        w.data = {**w.data, "waiting": {k: data[k] for k in ("party", "required", "next_check", "reason", "sent_information") if k in data}}
    elif action == "resume":
        role = require(db, actor, STAFF, w)
        if actor.person_id != w.owner_id or not w.status.startswith("waiting_"):
            raise AccessDenied("Возобновление доступно ответственному ожидающей работы.")
        if not data.get("due_at"):
            raise BusinessError("Укажите срок следующего действия.")
        w.due_at = parse_date(data["due_at"])
        if w.due_at <= now():
            raise BusinessError("Срок должен быть в будущем.")
        w.status = "in_progress"
    elif action == "handoff":
        role = require(db, actor, STAFF, w)
        if actor.person_id != w.owner_id or w.pending_owner_id:
            raise AccessDenied("Передачу инициирует текущий владелец без другой активной передачи.")
        target = db.get(Binding, data.get("binding_id"))
        if not target or target.purpose == "client" or not target.active or not target.verified:
            raise BusinessError("Новый маршрут не проверен.")
        if any(getattr(target, k) and getattr(target, k) != getattr(w, k)
               for k in ("partner_id", "brand_id", "establishment_id")):
            raise AccessDenied("Неверная область нового маршрута.")
        if not active_person_role(db, data["person_id"], STAFF, w):
            raise BusinessError("У нового исполнителя нет назначения.")
        w.pending_owner_id, w.pending_binding_id = data["person_id"], target.id
        queue(db, target, f"handoff:{w.id}:{old}", f"Предлагается принять работу {w.id}: {w.title}", work=w,
              buttons=[["Открыть", f"af:w:{w.id}"]])
    elif action == "handoff_accept":
        role = require(db, actor, STAFF, w)
        if actor.person_id != w.pending_owner_id or actor.binding_id != w.pending_binding_id:
            raise AccessDenied("Передачу принимает назначенный получатель в новом рабочем чате.")
        w.owner_id = w.pending_owner_id
        w.executor_id = w.owner_id
        w.binding_id = w.pending_binding_id
        w.pending_owner_id = w.pending_binding_id = None
    elif action == "reschedule":
        role = require(db, actor, STAFF, w)
        if actor.person_id not in {w.owner_id, w.reviewer_id} or not data.get("reason"):
            raise AccessDenied("Переносит срок текущий ответственный или принимающий с указанием причины.")
        changed = parse_date(data.get("due_at", ""))
        if changed <= now():
            raise BusinessError("Новый срок должен быть в будущем.")
        w.data = {**w.data, "deadline_change": {"reason": data["reason"], "previous": w.due_at.isoformat()}}
        w.due_at = changed
    elif action.startswith("sla_"):
        from ama.sla import action as sla_action
        role = sla_action(db, actor, w, action, data)
    elif action == "cancel":
        role = require(db, actor, {"curator", "support_head"}, w)
        if not data.get("reason"):
            raise BusinessError("Нужна причина отмены.")
        w.status, w.outcome = "done", "cancelled"
    else:
        raise BusinessError("Неизвестное действие.")
    from ama.sla import on_transition
    on_transition(db, actor, w, action, data)
    w.version += 1
    for t in db.scalars(select(Timer).where(Timer.work_id == w.id, Timer.status == "pending")):
        t.status = "cancelled"
    if w.status != "done":
        make_timer(db, w, "deadline", w.due_at)
    stamp(db, actor, w, action.upper(), old, data, role)
    queue(db, db.get(Binding, (w.review_binding_id or w.binding_id) if w.status == "review" else w.binding_id), f"state:{w.id}:{w.version}",
          f"{w.title}\n{STATUSES[w.status]}\nОтветственный: {w.owner_id}\nID: {w.id}", work=w,
          buttons=[["Открыть", f"af:w:{w.id}"]])
    if action == "accept" and w.status == "done":
        from ama.models import ProjectBlock, ProjectPlan
        for block in db.scalars(select(ProjectBlock).where(ProjectBlock.work_id == w.id)):
            block.status = "accepted"
            plan = db.get(ProjectPlan, block.project_id)
            plan.next_at = now()
            if block.code == "d180":
                plan.stage = "ongoing"
                plan.version += 1
                stamp(db, actor, plan, "STARTUP_CYCLE_COMPLETED", data={"work_id": w.id})
    from ama.packets import after_transition as packet_after
    packet_after(db,actor,w,action,data)
    db.add(Receipt(key=key, response={"id": w.id, "version": w.version}))
    return w


def attach(db, actor, work_id, platform, file_id, filename, source_key, album_id=None):
    w = db.scalar(select(Work).where(Work.id == work_id).with_for_update())
    if not w:
        raise BusinessError("Карточка не найдена. Выберите пункт перед отправкой файла.")
    require(db, actor, STAFF | {"partner", "observer"}, w)
    old = db.scalar(select(Attachment).where(Attachment.source_key == source_key))
    if old:
        return old
    if w.status == "done":
        raise BusinessError("Пункт завершён. Обратитесь к куратору.")
    source_binding = db.get(Binding, actor.binding_id)
    a = Attachment(work_id=w.id, author_id=actor.person_id, source_binding_id=actor.binding_id,
                   visibility="client" if source_binding.purpose == "client" else "internal",
                   platform=platform, file_id=file_id,
                   filename=filename[:250], source_key=source_key, album_id=album_id)
    db.add(a)
    db.flush()
    stamp(db, actor, a, "ATTACHMENT_RECEIVED", data={"work_id": w.id})
    return a  # Сохранение и доставка выполняются worker; никакой автоматической приёмки.


def training_action(db, actor, course_key, action, expected=None):
    from ama.digital import course_access
    assigned_course=course_access(db,actor,course_key)
    if not assigned_course:
        require(db, actor, {"partner"})
        if db.get(Binding, actor.binding_id).purpose != "client":
            raise AccessDenied("Ознакомление доступно из клиентского брендового чата.")
    course = record(db, "course", course_key)
    if not course.data.get("cards"):
        raise BusinessError("Карточки курса не опубликованы.")
    if course_key == "call_center" and not assigned_course:
        binding = db.get(Binding, actor.binding_id)
        partner = db.get(Entity, binding.partner_id)
        if not partner or not partner.data.get("call_center_applicable"):
            raise BusinessError("Ознакомление КЦ доступно после согласования услуги или персонального назначения в цифровом мастере.")
    t = db.scalar(select(Training).where(Training.person_id == actor.person_id, Training.course_key == course_key,
                                        Training.course_version == course.version).with_for_update())
    if not t:
        t = Training(person_id=actor.person_id, course_key=course_key, course_version=course.version,
                     cards=course.data["cards"], position=0, acknowledged=[], version=1)
        db.add(t)
        db.flush()
    if expected is not None:
        versioned(t, expected)
    if action == "next":
        card = t.cards[t.position]
        for material_key in card.get("required_materials", []):
            record(db, "material", material_key)
        t.acknowledged = sorted(set(t.acknowledged + [t.position]))
        if t.position < len(t.cards) - 1:
            t.position += 1
        elif len(t.acknowledged) == len(t.cards):
            t.completed_at = t.completed_at or now()
            stamp(db, actor, t, "TRAINING_ACKNOWLEDGED")
        t.paused = False
    elif action == "back":
        t.position = max(0, t.position - 1)
    elif action == "later":
        t.paused = True
    elif action != "open":
        raise BusinessError("Неизвестная команда ознакомления.")
    t.version += 1
    return t
