"""Confirmed buying interests, consultant actions and expert consultations (§1.0)."""

from copy import deepcopy
from datetime import timedelta, datetime
from types import SimpleNamespace
from sqlalchemy import select
from ama.auth import active_person_role
from ama.config import settings
from ama.errors import BusinessError, AccessDenied
from ama.models import (
    Lead,
    LeadTouch,
    SalesCase,
    SalesEvent,
    SalesControl,
    SalesExpert,
    Binding,
    Record,
    Deal,
    Outbox,
    Entity,
    now,
    uid,
)
from ama.service import versioned, parse_date, queue, stamp, record
from ama import reminders
from ama.ui import access

TERMINAL = {"success", "refused", "not_target"}
STAGE_LABELS = {
    "selection": "S0 · Подбор проекта",
    "new": "S0 · Подбор проекта",
    "qualification": "Квалификация",
    "presentation": "S1 · Знакомство с брендом",
    "financial_review": "S2 · Финансовый разбор",
    "terms": "S3 · Согласование условий",
    "contract": "S4 · Оформление договора",
    "signing": "S5 · На подписании",
    "payment_wait": "S6 · Подписан, ожидается оплата",
    "won": "S7 · Сделка состоялась",
}
STAGES = set(STAGE_LABELS)
CALL_RESULTS = {"connected", "no_answer", "call_later", "next_stage"}


def event(db, case, kind, data=None, actor=None, key=None):
    row = SalesEvent(
        event_key=key or uid(),
        lead_id=case.lead_id,
        case_id=case.id,
        actor_id=actor.person_id if actor else None,
        kind=kind,
        data={"owner_id": case.owner_id, "first_owner_id": case.data.get("first_owner_id"), **deepcopy(data or {})},
    )
    db.add(row)
    return row


def primary(db, lead):
    case = db.get(SalesCase, lead.id)
    if not case:
        case = SalesCase(
            id=lead.id,
            lead_id=lead.id,
            brand_id=lead.data.get("brand_id"),
            owner_id=lead.owner_id,
            stage=lead.stage,
            state=lead.state,
            result=lead.result,
            version=lead.version,
            created_at=lead.created_at,
            data=deepcopy(lead.data),
        )
        db.add(case)
        db.flush()
        for deal in db.scalars(select(Deal).where(Deal.lead_id == lead.id, Deal.case_id.is_(None))):
            deal.case_id = case.id
    return case


def sync(db, case):
    """Keep pre-0.3 APIs readable for the primary interest; never overwrite another purchase."""
    if case.id == case.lead_id:
        lead = db.get(Lead, case.lead_id)
        lead.owner_id, lead.stage, lead.state, lead.result = case.owner_id, case.stage, case.state, case.result
        lead.version += 1
        shared = {"contact", "silence", "next_contact", "reminder_case_id", "first_seen_at"}
        lead.data = {**lead.data, **{k: v for k, v in case.data.items() if k not in shared}, "brand_id": case.brand_id}


def get(db, case_id, lock=False):
    case = (
        db.scalar(select(SalesCase).where(SalesCase.id == case_id).with_for_update())
        if lock
        else db.get(SalesCase, case_id)
    )
    if not case:
        lead = db.get(Lead, case_id)
        if lead:
            case = primary(db, lead)
    if not case:
        raise BusinessError("Интерес не найден.")
    return case


def route(db, purpose):
    r = db.scalar(select(Record).where(Record.kind == "route", Record.key == purpose, Record.published.is_(True)))
    b = db.get(Binding, r.data.get("binding_id")) if r else None
    if not b or b.bot_id != settings().bot_id or b.purpose != purpose or not b.active or not b.verified:
        raise BusinessError(f"Не настроен проверенный маршрут {purpose}.")
    return r, b


def manager(db, actor):
    try:
        r, _ = route(db, "sales")
        return actor.person_id == (r.data.get("queue_manager_id") or r.data.get("owner_id")) and access.staff_role(
            db, actor, "consultant"
        )
    except BusinessError:
        return False


def can_case(db, actor, case, owned=True):
    binding = db.get(Binding, actor.binding_id)
    if not binding or binding.brand_id and binding.brand_id != case.brand_id:
        return False
    area = SimpleNamespace(partner_id=None, brand_id=case.brand_id, establishment_id=None)
    return access.scope(db, actor, area, {"consultant"}) and (
        not owned or case.owner_id in {None, actor.person_id} or manager(db, actor)
    )


def authorize(db, actor, case, owned=True):
    if not can_case(db, actor, case, owned):
        raise AccessDenied("Интерес назначен другому консультанту или вне области назначения.")
    if case.binding_id:
        b = db.get(Binding, case.binding_id)
        current = db.get(Binding, actor.binding_id)
        # A sales dashboard and its lead topic share a functional queue, not another group's data.
        if not b or not current or b.chat_id != current.chat_id or b.platform != current.platform:
            raise AccessDenied("Откройте интерес в его рабочем чате продаж.")


def sales_calendar(db):
    cal = reminders.configured_calendar(db)
    if not cal:
        raise BusinessError("Не опубликован календарь продаж.")
    return cal


def control(db, case, kind, due, token, **data):
    key = f"sales:{case.id}:{token}:{kind}"
    if not db.scalar(select(SalesControl).where(SalesControl.event_key == key)):
        db.add(SalesControl(case_id=case.id, kind=kind, due_at=due, event_key=key, data=data))


def controls(db, case, reason, start=None, due=None):
    """Store absolute deadlines: handoff never resets the initial clocks."""
    cal = sales_calendar(db)
    start = start or now()
    token = uid()
    if reason in {"accept", "first_reply"}:
        for minute in (10, 20):
            control(db, case, reason + str(minute), cal.add_minutes(start, minute), reason, anchor=start.isoformat())
    elif reason in {"wait", "not_now", "call"}:
        if reason == "call":
            for key, minute in (("call_before", -15), ("call_late", 15), ("call_escalate", 30)):
                control(db, case, key, working_shift(cal, due, minute), token, contact_token=case.data["contact_token"])
        else:
            before = working_shift(cal, due, -120) if reason == "wait" else previous_workday(cal, due)
            control(db, case, "contact_before", before, token, contact_token=case.data["contact_token"])
        control(db, case, "contact_due", due, token, contact_token=case.data["contact_token"])
    elif reason == "ready":
        for number in (1, 3):
            control(
                db, case, "ready_check", reminders.compute_due(cal, start, ("workdays", number)), token + str(number)
            )
        control(db, case, "ready_check", cal.add_minutes(start + timedelta(days=7), 0), token + "7")


def working_shift(cal, start, minutes):
    if minutes >= 0:
        return cal.add_minutes(start, minutes)
    # Walk backwards through actual working intervals (including holidays), preserving minute precision.
    from datetime import datetime, timezone

    local = start.replace(tzinfo=timezone.utc).astimezone(cal.tz)
    remaining = -minutes * 60
    for _ in range(4000):
        begin = datetime.combine(local.date(), cal.start, cal.tz)
        end = datetime.combine(local.date(), cal.end, cal.tz)
        if cal.working(local.date()) and local > begin:
            local = min(local, end)
            available = (local - begin).total_seconds()
            if remaining <= available:
                return (local - timedelta(seconds=remaining)).astimezone(timezone.utc).replace(tzinfo=None)
            remaining -= available
        local = datetime.combine(local.date() - timedelta(days=1), cal.end, cal.tz)
    raise BusinessError("Не удалось рассчитать контрольную дату.")


def previous_workday(cal, due):
    from datetime import datetime, timezone

    local = due.replace(tzinfo=timezone.utc).astimezone(cal.tz)
    day = local.date() - timedelta(days=1)
    for _ in range(4000):
        if cal.working(day):
            point = datetime.combine(day, min(max(local.time(), cal.start), cal.end), cal.tz)
            return point.astimezone(timezone.utc).replace(tzinfo=None)
        day -= timedelta(days=1)
    raise BusinessError("Не найден предыдущий рабочий день.")


def card(db, case):
    lead = db.get(Lead, case.lead_id)
    brand = db.get(Entity, case.brand_id) if case.brand_id else None
    d = case.data
    details = "\n".join(
        label + ": " + str(d[key])
        for key, label in {
            "preferred_time": "Удобное время",
            "business": "Действующий бизнес",
            "premises": "Помещение",
            "project_type": "Тип проекта",
            "start_period": "Планируемый старт",
            "experience": "Опыт",
            "location": "Локация",
        }.items()
        if d.get(key)
    )
    if d.get("seen"):
        from ama.prospects import CATEGORIES

        details += "\nРазделы в боте: " + ", ".join(CATEGORIES.get(k, k) for k in d["seen"])
    if d.get("issued"):
        details += "\nЗапрошенные материалы: " + ", ".join(
            str(x["id"]) + " v" + str(x["version"]) for x in d["issued"][-10:]
        )
    if d.get("questions"):
        details += "\nВопросы до передачи: " + "; ".join(d["questions"][-5:])
    return (
        f"{d.get('name', 'Лид')} · {brand.name if brand else 'Проект не выбран'}\n"
        f"lead_id: {lead.id}\ninterest_id: {case.id}\nrequest_id: {d.get('request_id', case.id)}\n"
        f"Город: {d.get('city', 'не указан')}\nИсточник: {d.get('source', lead.source)}\n"
        f"Связь: {d.get('communication', 'чат')} · {d.get('phone', '')}\n"
        f"Этап: {STAGE_LABELS.get(case.stage, case.stage)} · Состояние: {case.state} · Исход: {case.result or 'активен'}\n"
        f"Следующий контакт: {d.get('next_contact', 'не назначен')}\n"
        f"Последний запрос: {d.get('last_message', '')[:1000]}\n{details}"
    )


def announce(db, case, purpose="sales", expert=None, deal=None):
    _, base = route(db, purpose)
    target = expert or deal or case
    if purpose == "sales":
        case.binding_id = base.id
    if base.platform == "telegram" and not base.topic_key:
        db.add(
            Outbox(
                dedupe_key=f"sales-topic:{purpose}:{target.id}",
                platform=base.platform,
                chat_id=base.chat_id,
                binding_id=base.id,
                binding_version=base.version,
                payload={
                    "op": "create_topic",
                    "topic_name": ("Лид " if not expert and not deal else "Консультация " if expert else "Договор ")
                    + target.id[:12],
                    "topic_target": "expert" if expert else "deal" if deal else "case",
                    "target_id": target.id,
                    "case_id": case.id,
                    "card_text": card(db, case) + (f"\nВопрос: {expert.question}" if expert else ""),
                },
            )
        )
    else:
        queue(
            db,
            base,
            f"sales-card:{purpose}:{target.id}",
            card(db, case) + (f"\nВопрос: {expert.question}" if expert else ""),
        )


def on_topic(db, row, topic_id):
    base = db.get(Binding, row.binding_id)
    p = row.payload
    case = db.get(SalesCase, p["case_id"])
    if not base or not case or base.version != row.binding_version or not topic_id:
        raise BusinessError("Маршрут изменился во время создания темы; нужна сверка.")
    topic = str(int(topic_id))
    binding = db.scalar(
        select(Binding).where(
            Binding.bot_id == base.bot_id,
            Binding.platform == base.platform,
            Binding.chat_id == base.chat_id,
            Binding.topic_key == topic,
        )
    )
    if not binding:
        binding = Binding(
            bot_id=base.bot_id,
            platform=base.platform,
            chat_id=base.chat_id,
            topic_key=topic,
            purpose=base.purpose,
            brand_id=None
            if p["topic_target"] == "deal" and len(db.get(Deal, p["target_id"]).data.get("case_ids", [])) > 1
            else case.brand_id,
            active=True,
            verified=True,
        )
        db.add(binding)
        db.flush()
    kind = p["topic_target"]
    if kind == "case":
        case.binding_id = binding.id
    elif kind == "expert":
        obj = db.get(SalesExpert, p["target_id"])
        obj.binding_id = binding.id
    else:
        obj = db.get(Deal, p["target_id"])
        obj.data = {**obj.data, "binding_id": binding.id}
    queue(db, binding, "sales-topic-card:" + row.id, p["card_text"])


def submit(db, lead, profile, brand_id=None, parallel=False, evidence=None, case_id=None):
    db.scalar(select(Lead).where(Lead.id == lead.id).with_for_update())
    case = get(db, case_id, True) if case_id else primary(db, lead)
    if case.lead_id != lead.id:
        raise AccessDenied("Интерес принадлежит другому человеку.")
    if parallel:
        if not evidence:
            raise BusinessError("Нужно явное подтверждение нового параллельного интереса.")
        case = SalesCase(lead_id=lead.id, brand_id=brand_id, parent_id=case.id, data={})
        db.add(case)
        db.flush()
    elif brand_id and case.brand_id and case.brand_id != brand_id:
        raise BusinessError(
            "Другой бренд требует явного подтверждения отдельного интереса; прежний интерес сохраняется."
        )
    elif case.result == "success":
        raise BusinessError("Сделка уже состоялась. Для новой покупки подтвердите отдельный интерес.")
    if case.result in {"refused", "not_target"}:
        case.data = {**case.data, "reply_to_inbound": True}
    if brand_id:
        brand = db.get(Entity, brand_id)
        if not brand or not brand.data.get("sales_available"):
            raise BusinessError("Продажи этого проекта не активированы.")
        case.brand_id = brand_id
    first = not case.data.get("submitted_at")
    if profile.get("consent_record_id") and case.result in {"refused", "paused"}:
        case.result, case.state = None, "active"
    case.data = {
        **case.data,
        **profile,
        "brand_id": case.brand_id,
        "submitted_at": case.data.get("submitted_at", now().isoformat()),
        "request_id": case.data.get("request_id", uid()),
    }
    lead.data = {
        **lead.data,
        "reminder_case_id": case.id,
        **(
            {"first_seen_at": profile["first_seen_at"]}
            if profile.get("first_seen_at") and not lead.data.get("first_seen_at")
            else {}
        ),
    }
    if first:
        controls(db, case, "accept", start=datetime.fromisoformat(case.data["submitted_at"]))
        announce(db, case)
        event(db, case, "CONSULTANT_REQUESTED", {"callback": profile.get("communication") == "call"})
    elif profile.get("consent_record_id"):
        case.data = {**case.data, "requires_contact": True}
        if case.binding_id:
            queue(
                db,
                db.get(Binding, case.binding_id),
                "sales-renewed:" + uid(),
                "Повторное явное обращение клиента.\n" + card(db, case),
            )
        event(db, case, "CONSULTANT_REQUEST_RENEWED")
    case.version += 1
    sync(db, case)
    return case


def cancel_automatic(db, lead):
    for touch in db.scalars(
        select(LeadTouch).where(LeadTouch.lead_id == lead.id, LeadTouch.status.in_(["pending", "queued"]))
    ):
        touch.status = "cancelled"
    for row in db.scalars(select(Outbox).where(Outbox.status == "pending")):
        if row.payload.get("lead_id") == lead.id and row.payload.get("lead_touch_id"):
            row.status = "cancelled"


def schedule_contact(db, case, lead, data, reason="wait"):
    due = parse_date(data["next_contact"])
    if due <= now():
        raise BusinessError("Дата контакта должна быть в будущем.")
    case.data = {
        **case.data,
        "next_contact": data["next_contact"],
        "next_action": data.get("next_action") or data.get("comment") or "contact",
        "contact_token": uid(),
        "contact_reason": data.get("reason", ""),
        "requires_contact": False,
    }
    lead.data = {**lead.data, "next_contact": data["next_contact"], "reminder_case_id": case.id}
    cancel_automatic(db, lead)
    controls(db, case, reason, due=due)


def action(db, actor, case_id, expected, name, data, key):
    case = get(db, case_id, True)
    authorize(db, actor, case)
    versioned(case, expected)
    lead = db.get(Lead, case.lead_id)
    if db.scalar(select(SalesEvent).where(SalesEvent.event_key == key)):
        return {"id": case.id, "version": case.version}
    if name == "reassign":
        if not manager(db, actor) or not data.get("reason"):
            raise AccessDenied("Переназначает ответственный за очередь с основанием.")
        area = SimpleNamespace(partner_id=None, establishment_id=None, brand_id=case.brand_id)
        if not active_person_role(db, data["person_id"], {"consultant"}, area):
            raise BusinessError("Новый консультант не назначен в этой области.")
        for pending in db.scalars(
            select(SalesControl).where(
                SalesControl.case_id == case.id, SalesControl.kind.like("accept%"), SalesControl.status == "pending"
            )
        ):
            pending.status = "cancelled"
        previous_owner = case.owner_id
        case.owner_id = data["person_id"]
        data = {**data, "previous_owner": previous_owner}
        case.data = {
            **case.data,
            "needs_accept": True,
            "first_assigned_at": case.data.get("first_assigned_at", now().isoformat()),
            "first_owner_id": case.data.get("first_owner_id", case.owner_id),
        }
        case.state = "assigned"
        anchor = datetime.fromisoformat(case.data.get("submitted_at", now().isoformat()))
        cal = sales_calendar(db)
        for minute in (10, 20):
            control(db, case, "accept" + str(minute), cal.add_minutes(anchor, minute), key)
    elif name == "take":
        if case.owner_id not in {None, actor.person_id}:
            raise AccessDenied("Лид уже принят другим консультантом.")
        if (
            case.owner_id == actor.person_id
            and case.data.get("accepted_by") == actor.person_id
            and not case.data.get("needs_accept")
        ):
            return {"id": case.id, "version": case.version}
        case.owner_id = actor.person_id
        case.data = {
            **case.data,
            "accepted_by": actor.person_id,
            "needs_accept": False,
            "first_assigned_at": case.data.get("first_assigned_at", now().isoformat()),
            "first_owner_id": case.data.get("first_owner_id", actor.person_id),
            "requires_contact": True,
        }
        case.state = "active"
        if not case.data.get("accepted_at"):
            case.data = {**case.data, "accepted_at": now().isoformat()}
            controls(db, case, "first_reply")
    else:
        if case.owner_id != actor.person_id:
            raise AccessDenied("Действие выполняет текущий владелец интереса.")
        if case.data.get("needs_accept"):
            raise BusinessError("Сначала примите назначенный интерес в работу.")
        if case.result == "success" and name not in {"note"}:
            raise BusinessError("Успешная сделка не переоткрывается. Создайте подтверждённый связанный интерес.")
        if case.stage in {"contract", "signing", "payment_wait"} and name not in {"reply", "note", "expert"}:
            raise BusinessError("Идёт договорный этап. Подписи и расчёты ведёт договорный ответственный.")
        if name == "qualify":
            allowed = {
                "name",
                "city",
                "phone",
                "location",
                "budget",
                "start_period",
                "experience",
                "premises",
                "business",
                "commercial_key",
                "commercial_version",
                "intent_confirmed",
                "requisites",
                "request_requisites",
                "exceptions",
                "communication",
                "economics_acknowledged",
                "exception_reference",
            }
            case.data = {**case.data, **{k: v for k, v in data.items() if k in allowed}}
            if data.get("brand_id") and data["brand_id"] != case.brand_id:
                raise BusinessError("Выбор бренда подтверждается отдельным действием «Назначить проект».")
            if case.stage in {"new", "selection", "qualification"}:
                case.stage = "presentation" if case.brand_id else "selection"
        elif name == "assign_brand":
            if not data.get("evidence"):
                raise BusinessError("Нужно подтверждение выбора бренда лидом.")
            brand = db.get(Entity, data["brand_id"])
            if not brand or not brand.data.get("sales_available"):
                raise BusinessError("Продажи проекта не активированы.")
            if case.brand_id and case.brand_id != brand.id:
                raise BusinessError(
                    "Назначение проекта заполняет общий интерес без бренда. Для другого бренда подтвердите отдельный интерес."
                )
            case.brand_id = brand.id
            authorize(db, actor, case)
            case.data = {**case.data, "brand_id": brand.id, "brand_confirmation": data["evidence"]}
        elif name == "stage":
            if data.get("stage") not in {"selection", "presentation", "financial_review", "terms"} or not data.get(
                "reason"
            ):
                raise BusinessError("Финансовый разбор и договор подтверждаются отдельными действиями.")
            case.stage = data["stage"]
        elif name == "financial_review":
            if not data.get("evidence"):
                raise BusinessError("Нужно подтверждение состоявшегося разбора, а не отправки файла.")
            case.stage = "financial_review"
            case.data = {**case.data, "financial_review": {"evidence": data["evidence"], "at": now().isoformat()}}
        elif name in {"wait_lead", "not_now", "contact", "call_schedule"}:
            schedule_contact(
                db, case, lead, data, "call" if name == "call_schedule" else "not_now" if name == "not_now" else "wait"
            )
            case.state = (
                "not_now" if name == "not_now" else "waiting_lead" if name == "wait_lead" else "contact_scheduled"
            )
            if name == "not_now":
                case.result = "paused"
        elif name == "call":
            if data.get("result") not in CALL_RESULTS or not data.get("comment"):
                raise BusinessError("Зафиксируйте фактический результат звонка и следующий шаг.")
            case.data = {
                **case.data,
                "first_response_at": case.data.get("first_response_at", now().isoformat()),
                "last_call_at": now().isoformat(),
                "contact_token": uid(),
            }
            if data["result"] == "no_answer":
                misses = case.data.get("missed_calls", 0) + 1
                case.data = {**case.data, "missed_calls": misses}
                if misses >= 3:
                    due = sales_calendar(db).add_minutes(now() + timedelta(days=14), 0)
                    case.state = "waiting_lead"
                else:
                    days = 1 if misses == 1 else 3
                    due = reminders.compute_due(sales_calendar(db), now(), ("workdays", days))
                data = {
                    **data,
                    "next_contact": data.get("next_contact") or due.isoformat() + "+00:00",
                    "next_action": "Проверить после недозвона" if misses >= 3 else "Повторить звонок",
                }
                schedule_contact(db, case, lead, data)
                case.state = "waiting_lead" if misses >= 3 else "contact_scheduled"
                if misses == 1:
                    reminders.schedule(db, lead, "missed_call")
            else:
                if not data.get("next_contact"):
                    raise BusinessError("После звонка укажите следующую контрольную дату.")
                schedule_contact(db, case, lead, data)
                case.data = {**case.data, "missed_calls": 0}
                case.state = "active"
        elif name in {"refuse", "not_target"}:
            if not data.get("reason"):
                raise BusinessError("Укажите фактическую причину.")
            case.result, case.state = ("refused" if name == "refuse" else "not_target"), "closed"
            cancel_automatic(db, lead)
            if name == "refuse":
                lead.no_contact = True
        elif name == "reopen":
            if not data.get("consent_evidence"):
                raise BusinessError("Нужно подтверждение согласия на возобновление контакта.")
            lead.no_contact, case.result, case.state = False, None, "active"
            # An internal checkbox never resets the client's silence allowance.
        elif name == "reply":
            if data.get("knowledge_id"):
                from ama.prospects import knowledge

                row = next((r for r in knowledge(db, case.brand_id) if r.id == data["knowledge_id"]), None)
                if not row or row.version != data.get("knowledge_version"):
                    raise BusinessError("Публичная редакция материала изменилась. Перечитайте базу.")
            if lead.no_contact and not case.data.get("reply_to_inbound"):
                raise AccessDenied("Контакты запрещены. Ответ допускается только на новое явное обращение.")
            contact = lead.data.get("contact")
            if not isinstance(contact, dict) or not contact.get("chat_id"):
                raise BusinessError("Нет подтверждённого личного чата.")
            if not data.get("text"):
                raise BusinessError("Введите клиентский ответ.")
            # A manual response replaces reminders but retains the current epoch/sent count.
            cancel_automatic(db, lead)
            msg = Outbox(
                dedupe_key="sales-reply:" + key,
                platform=contact["platform"],
                chat_id=contact["chat_id"],
                payload={
                    "text": data["text"],
                    "lead_id": lead.id,
                    "case_id": case.id,
                    "sales_reply": True,
                    "sales_actor": actor.person_id,
                    "sales_actor_binding": actor.binding_id,
                    "sales_grants": access.grants_hash(db, actor),
                    "recipient_contact": deepcopy(contact),
                    **(
                        {"public_record": data["knowledge_id"], "public_record_version": data["knowledge_version"]}
                        if data.get("knowledge_id")
                        else {}
                    ),
                    "reply_to_inbound": bool(case.data.get("reply_to_inbound")),
                    "touch_reason": data.get("touch_reason", "consultant_reply"),
                    "silence_epoch": lead.data.get("silence", {}).get("epoch"),
                },
            )
            db.add(msg)
            case.data = {**case.data, "reply_to_inbound": False}
        elif name == "expert":
            r, b = route(db, "chief_marketer")
            area = SimpleNamespace(partner_id=None, brand_id=case.brand_id, establishment_id=None)
            owner = r.data.get("owner_id")
            if not active_person_role(db, owner, {"chief_marketer"}, area):
                raise BusinessError("Не назначен Главный маркетолог в этой области.")
            pending = db.scalar(
                select(SalesExpert).where(SalesExpert.case_id == case.id, SalesExpert.state.in_(["new", "in_progress"]))
            )
            if pending:
                raise BusinessError("Уже есть открытый внутренний вопрос. Продолжите его без дубля.")
            due = parse_date(data["due_at"])
            if due <= now() or not data.get("question"):
                raise BusinessError("Нужны вопрос и будущая контрольная дата.")
            expert = SalesExpert(
                case_id=case.id,
                requester_id=actor.person_id,
                owner_id=owner,
                binding_id=b.id,
                question=data["question"],
                category=data.get("category", "question"),
                due_at=due,
                data={"profile": deepcopy(case.data), "brand_id": case.brand_id},
            )
            db.add(expert)
            db.flush()
            case.state = "waiting_chief_marketer"
            case.data = {**case.data, "expert_id": expert.id}
            announce(db, case, "chief_marketer", expert=expert)
            control(db, case, "expert_due", due, expert.id, expert_id=expert.id)
        elif name == "ready":
            readiness(db, case)
            if not case.data.get("ready_at"):
                case.data = {**case.data, "ready_at": now().isoformat()}
                controls(db, case, "ready")
        elif name == "contract":
            route(db, "contracts")
            readiness(db, case)
            existing = db.scalar(select(Deal).where(Deal.case_id == case.id))
            if existing:
                return {"id": existing.id, "version": existing.version}
            from ama.models import DealInterest

            cases = [case]
            for other_id in sorted(set(data.get("additional_case_ids", [])) - {case.id}):
                other = get(db, other_id, True)
                authorize(db, actor, other)
                if other.lead_id != lead.id or other.owner_id != actor.person_id or other.result in TERMINAL:
                    raise BusinessError("Объединяются только подтверждённые интересы того же человека и консультанта.")
                readiness(db, other)
                cases.append(other)
            if any(db.scalar(select(DealInterest).where(DealInterest.case_id == c.id)) for c in cases):
                raise BusinessError("Один из интересов уже включён в другой договорный комплект.")
            case_input_versions = {c.id: c.version for c in cases}
            commercial = record(db, "commercial", case.data["commercial_key"])
            snapshots = [record(db, "commercial", c.data["commercial_key"]) for c in cases]
            deal = Deal(
                lead_id=lead.id,
                case_id=case.id,
                required_minor=sum(r.data["required_minor"] for r in snapshots),
                data={
                    "commercial_id": commercial.id,
                    "commercial_version": commercial.version,
                    "commercial_snapshot": deepcopy(commercial.data),
                    "brand_id": case.brand_id,
                    "case_ids": [c.id for c in cases],
                    "commercial_snapshots": [deepcopy(r.data) for r in snapshots],
                    "profile": deepcopy(case.data),
                    "consultant_id": actor.person_id,
                    "deal_type": "expansion" if case.parent_id else "new",
                },
            )
            db.add(deal)
            db.flush()
            for item in cases:
                db.add(DealInterest(deal_id=deal.id, case_id=item.id))
                item.stage, item.state = "contract", "waiting_contracts"
                if item.id != case.id:
                    item.version += 1
                    sync(db, item)
            cancel_automatic(db, lead)
            announce(db, case, "contracts", deal=deal)
            event(db, case, "CONTRACT_STARTED", {"deal_id": deal.id}, actor)
        elif name == "note":
            if not data.get("text"):
                raise BusinessError("Введите внутреннюю заметку.")
        else:
            raise BusinessError("Неизвестное действие продажи.")
    case.version += 1
    sync(db, case)
    event(db, case, name.upper(), data, actor, key)
    return {
        "id": case.id,
        "version": case.version,
        **(
            {"deal_id": deal.id, "contract_amount_minor": deal.required_minor, "case_versions": case_input_versions}
            if name == "contract"
            else {}
        ),
        **(
            {
                "reply_contact": deepcopy(lead.data.get("contact")),
                "reply_epoch": lead.data.get("silence", {}).get("epoch"),
            }
            if name == "reply"
            else {}
        ),
    }


def readiness(db, case):
    d = case.data
    needed = [k for k in ("name", "city", "commercial_key", "intent_confirmed") if not d.get(k)]
    lead = db.get(Lead, case.lead_id)
    if (
        needed
        or not case.brand_id
        or not lead.data.get("contact")
        or not (d.get("requisites") or d.get("request_requisites"))
    ):
        raise BusinessError(
            "Недостаточно данных для договора: " + ", ".join(needed + ["бренд/контакт/реквизиты — проверьте полноту"])
        )
    commercial = record(db, "commercial", d["commercial_key"])
    if commercial.data.get("brand_id") != case.brand_id or commercial.version != d.get("commercial_version"):
        raise BusinessError("Подтвердите текущую коммерческую редакцию выбранного проекта.")
    if not d.get("economics_acknowledged"):
        raise BusinessError("Зафиксируйте ознакомление с экономикой проекта.")
    if d.get("exceptions"):
        expert = db.get(SalesExpert, d.get("exception_reference")) if d.get("exception_reference") else None
        if (
            not expert
            or expert.case_id != case.id
            or expert.state != "answered"
            or not expert.data.get("approved_exception")
        ):
            raise BusinessError(
                "Индивидуальные условия требуют явного подтверждения Главного маркетолога по этому интересу."
            )
    from ama.prospects import approved

    if not approved(commercial):
        raise BusinessError("Коммерческая редакция не утверждена или больше не действует.")


def expert_action(db, actor, expert_id, expected, name, data):
    expert = db.scalar(select(SalesExpert).where(SalesExpert.id == expert_id).with_for_update())
    if not expert:
        raise BusinessError("Консультация не найдена.")
    case = get(db, expert.case_id)
    area = SimpleNamespace(partner_id=None, establishment_id=None, brand_id=case.brand_id)
    if (
        not access.scope(db, actor, area, {"chief_marketer"})
        or actor.person_id != expert.owner_id
        or actor.binding_id != expert.binding_id
    ):
        raise AccessDenied("Отвечает назначенный Главный маркетолог в теме консультации.")
    versioned(expert, expected)
    if expert.state == "answered":
        raise BusinessError("Ответ уже сохранён.")
    if name == "take":
        expert.state = "in_progress"
    elif name == "answer":
        if not data.get("answer"):
            raise BusinessError("Введите внутренний ответ консультанту.")
        expert.answer, expert.state = data["answer"], "answered"
        expert.data = {
            **expert.data,
            "approved_exception": bool(expert.category == "terms" and data.get("approved_exception")),
        }
        if case.result not in TERMINAL:
            case.state = "active"
            case.data = {**case.data, "requires_contact": True}
        if case.binding_id:
            queue(
                db,
                db.get(Binding, case.binding_id),
                f"expert-answer:{expert.id}:{expert.version}",
                "Ответ Главного маркетолога (внутренний):\n"
                + expert.answer
                + "\nКлиентский ответ отправляет консультант после предпросмотра.",
            )
        event(db, case, "EXPERT_ANSWERED", {"expert_id": expert.id}, actor)
    else:
        raise BusinessError("Неизвестное действие консультации.")
    expert.version += 1
    stamp(db, actor, expert, "SALES_EXPERT_" + name.upper(), expected, role="chief_marketer")
    return {"id": expert.id, "version": expert.version}


def tick(db):
    row = db.scalar(
        select(SalesControl)
        .where(SalesControl.status == "pending", SalesControl.due_at <= now(), SalesControl.retry_at <= now())
        .order_by(SalesControl.due_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if not row:
        return False
    case = get(db, row.case_id, True)
    active = case.result not in TERMINAL
    d, kind = case.data, row.kind
    if kind.startswith("accept"):
        active &= not case.owner_id or bool(d.get("needs_accept"))
    elif kind.startswith("first_reply"):
        active &= (
            bool(case.owner_id)
            and not d.get("first_response_at")
            and case.state not in {"waiting_lead", "not_now", "waiting_contracts"}
        )
    elif kind.startswith(("contact_", "call_")):
        active &= d.get("contact_token") == row.data.get("contact_token")
    elif kind == "ready_check":
        active &= case.stage not in {"contract", "signing", "payment_wait", "won"}
    elif kind == "expert_due":
        expert = db.get(SalesExpert, row.data["expert_id"])
        active &= bool(expert and expert.state != "answered")
    if not active:
        row.status = "cancelled"
        return True
    try:
        cal = sales_calendar(db)
        permitted = cal.add_minutes(now(), 0)
        if permitted > now():
            row.retry_at = permitted
            return True
        _, base = route(db, "sales")
    except BusinessError:
        row.retry_at = now() + timedelta(minutes=5)
        row.data = {**row.data, "configuration_missing": True}
        return True
    target = db.get(Binding, case.binding_id) if case.binding_id else base
    if kind in {"accept20", "first_reply20", "call_escalate"}:
        target = base
    if kind == "contact_due":
        case.state = "active"
        if case.result == "paused":
            case.result = None
        case.data = {**d, "requires_contact": True, "next_contact": None}
        lead = db.get(Lead, case.lead_id)
        if lead.data.get("reminder_case_id") == case.id:
            lead.data = {**lead.data, "next_contact": None}
        case.version += 1
        sync(db, case)
    labels = {
        "accept10": "Лид не принят 10 рабочих минут",
        "accept20": "Контроль очереди: лид не принят 20 рабочих минут",
        "first_reply10": "Нет первого ответа 10 рабочих минут",
        "first_reply20": "Контроль очереди: нет первого ответа 20 рабочих минут",
        "call_before": "До назначенного звонка 15 рабочих минут",
        "call_late": "Не зафиксирован результат звонка",
        "call_escalate": "Контроль очереди: звонок без результата",
        "contact_before": "Приближается согласованный контакт",
        "contact_due": "Требуется контакт: согласованная дата наступила",
        "ready_check": "Проверьте следующий шаг до передачи в договоры",
        "expert_due": "Просрочена внутренняя консультация Главного маркетолога",
        "after_silence": "Два напоминания доставлены. Нужна внутренняя контрольная точка",
    }
    queue(db, target, "sales-control:" + row.id, labels.get(kind, kind) + "\n" + card(db, case))
    row.status = "done"
    event(db, case, "CONTROL_" + kind.upper(), {"due_at": row.due_at.isoformat()})
    return True
