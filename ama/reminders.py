"""One person-wide silence chain; at most two delivered client reminders (§1.0У)."""

from datetime import timedelta, datetime, timezone
from sqlalchemy import select
from ama.models import LeadTouch, Outbox, Record, SalesCase, now, uid
from ama.calendar import Calendar
from ama.errors import BusinessError

RULES = {
    "onboarding": (("hours", 24), ("hours", 48)),
    "auto_answer": (("hours", 24), ("hours", 72)),
    "consultant_reply": (("workdays", 1), ("workdays", 3)),
    "clarification": (("workdays", 1), ("workdays", 3)),
    "call_time": (("workhours", 2), ("next_start", 1)),
    "materials": (("workdays", 1), ("hours", 96)),
    "missed_call": (("hours", 0), ("hours", 0)),
}
PRIORITY = {
    "missed_call": 0,
    "call_time": 1,
    "clarification": 2,
    "consultant_reply": 2,
    "auto_answer": 3,
    "materials": 4,
    "onboarding": 5,
}


def react(db, lead, contact=None):
    for touch in db.scalars(
        select(LeadTouch).where(LeadTouch.lead_id == lead.id, LeadTouch.status.in_(["pending", "queued"]))
    ):
        touch.status = "cancelled"
    for msg in db.scalars(select(Outbox).where(Outbox.status == "pending")):
        if msg.payload.get("lead_touch_id") and msg.payload.get("lead_id") == lead.id:
            msg.status = "cancelled"
    lead.last_reaction_at = now()
    lead.data = {**lead.data, "silence": {"epoch": uid(), "sent": 0}, **({"contact": contact} if contact else {})}


def compute_due(cal, start, rule):
    kind, n = rule
    if kind == "hours":
        return cal.add_minutes(start + timedelta(hours=n), 0)
    if kind == "workhours":
        return cal.add_minutes(start, n * 60)
    local = start.replace(tzinfo=timezone.utc).astimezone(cal.tz)
    day = local.date()
    for _ in range(4000):
        day += timedelta(days=1)
        if cal.working(day):
            n -= 1
            if n == 0:
                clock = cal.start if kind == "next_start" else local.timetz().replace(tzinfo=None)
                candidate = datetime.combine(day, clock, cal.tz).astimezone(timezone.utc).replace(tzinfo=None)
                return cal.add_minutes(candidate, 0)
    raise BusinessError("Не удалось назначить касание.")


def configured_calendar(db):
    route = db.scalar(select(Record).where(Record.kind == "route", Record.key == "sales", Record.published.is_(True)))
    key = route.data.get("calendar", "sales") if route else "sales"
    row = db.scalar(select(Record).where(Record.kind == "calendar", Record.key == key, Record.published.is_(True)))
    return Calendar(row.data) if row else None


def active_case(db, lead):
    case_id = lead.data.get("reminder_case_id")
    return db.get(SalesCase, case_id) if case_id else None


def allowed(db, lead, reason):
    case = active_case(db, lead)
    stage, result = (case.stage, case.result) if case else (lead.stage, lead.result)
    return (
        not lead.no_contact
        and stage not in {"contract", "signing", "payment_wait", "won"}
        and result not in {"success", "refused", "not_target"}
        and (not lead.data.get("next_contact") or reason == "missed_call")
    )


def template(db, lead, reason, ordinal):
    case = active_case(db, lead)
    if case and case.brand_id:
        row = db.scalar(
            select(Record).where(
                Record.kind == "template",
                Record.key == f"lead.{case.brand_id}.{reason}.{ordinal}",
                Record.published.is_(True),
            )
        )
        if row:
            return row
    return db.scalar(
        select(Record).where(
            Record.kind == "template", Record.key == f"lead.{reason}.{ordinal}", Record.published.is_(True)
        )
    )


def schedule(db, lead, reason, start=None):
    if reason not in RULES or not allowed(db, lead, reason):
        return None
    silence = lead.data.get("silence", {})
    if not silence.get("epoch"):
        react(db, lead)
        silence = lead.data["silence"]
    sent = silence.get("sent", 0)
    if sent >= 2:
        return None
    calendar = configured_calendar(db)
    text = template(db, lead, reason, sent + 1)
    if not calendar or not text or not text.data.get("text"):
        return None
    pending = db.scalar(
        select(LeadTouch)
        .where(LeadTouch.lead_id == lead.id, LeadTouch.epoch == silence["epoch"], LeadTouch.ordinal == sent + 1)
        .with_for_update()
    )
    if pending and pending.status in {"pending", "queued"}:
        if pending.reason == reason or PRIORITY[pending.reason] < PRIORITY[reason]:
            return pending
    if pending:
        if pending.status == "sent":
            return None
        for msg in db.scalars(select(Outbox).where(Outbox.status == "pending")):
            if msg.payload.get("lead_touch_id") == pending.id:
                msg.status = "cancelled"
        pending.reason, pending.status, pending.version = reason, "pending", pending.version + 1
        pending.due_at = compute_due(calendar, start or now(), RULES[reason][sent])
        return pending
    t = LeadTouch(
        lead_id=lead.id,
        epoch=silence["epoch"],
        ordinal=sent + 1,
        reason=reason,
        due_at=compute_due(calendar, start or now(), RULES[reason][sent]),
    )
    db.add(t)
    return t


def deliverable(db, lead, payload):
    touch = db.get(LeadTouch, payload.get("lead_touch_id"))
    return bool(
        touch
        and touch.status == "queued"
        and payload.get("touch_version", touch.version) == touch.version
        and payload.get("silence_epoch") == lead.data.get("silence", {}).get("epoch")
        and lead.data.get("silence", {}).get("sent", 0) < 2
        and allowed(db, lead, touch.reason)
    )


def message_count(db, lead, statuses):
    epoch = lead.data.get("silence", {}).get("epoch")
    if not epoch:
        return 0
    rows = db.scalars(
        select(Outbox).where(
            Outbox.status.in_(statuses),
            Outbox.payload["lead_id"].as_string() == lead.id,
            Outbox.payload["silence_epoch"].as_string() == epoch,
        )
    )
    return sum(bool(row.payload.get("lead_touch_id")) for row in rows)


def on_delivered(db, row):
    from ama.models import Lead

    lead_id = row.payload.get("lead_id")
    if not lead_id:
        return
    lead = db.scalar(select(Lead).where(Lead.id == lead_id).with_for_update())
    if not lead:
        return
    if row.payload.get("public_record"):
        from ama.models import SalesEvent

        if not db.scalar(select(SalesEvent).where(SalesEvent.event_key == "material-delivered:" + row.id)):
            db.add(
                SalesEvent(
                    event_key="material-delivered:" + row.id,
                    lead_id=lead.id,
                    case_id=row.payload.get("case_id"),
                    kind="CONTENT_DELIVERED",
                    data={
                        "record_id": row.payload["public_record"],
                        "version": row.payload.get("public_record_version"),
                        "read_confirmed": False,
                    },
                )
            )
    case = db.get(SalesCase, row.payload.get("case_id")) if row.payload.get("case_id") else None
    if row.payload.get("sales_reply") and case:
        case.data = {
            **case.data,
            "first_response_at": case.data.get("first_response_at", now().isoformat()),
            "requires_contact": False
            if row.payload.get("silence_epoch") == lead.data.get("silence", {}).get("epoch")
            else case.data.get("requires_contact", True),
        }
        case.version += 1
        from ama.sale_flow import event

        event(db, case, "CONSULTANT_REPLY_DELIVERED", {"outbox_id": row.id})
    if lead.no_contact:
        return
    if row.payload.get("lead_touch_id"):
        t = db.get(LeadTouch, row.payload["lead_touch_id"])
        if not t or (t.status != "queued" and row.status != "sent"):
            return
        t.status = "sent"
        silence = lead.data.get("silence", {})
        if silence.get("epoch") != t.epoch:
            return
        lead.data = {
            **lead.data,
            "silence": {**silence, "sent": max(silence.get("sent", 0), message_count(db, lead, ["sent"]), t.ordinal)},
        }
        if lead.data["silence"]["sent"] < 2 and t.reason != "missed_call":
            schedule(db, lead, t.reason)
        elif lead.data["silence"]["sent"] >= 2:
            current = active_case(db, lead)
            if current and current.data.get("submitted_at"):
                from ama.sale_flow import control

                control(db, current, "after_silence", now(), t.epoch)
                current.state = "waiting_without_auto"
            elif not current:
                lead.state = "waiting_without_auto"
    elif row.payload.get("touch_reason") and row.payload.get("silence_epoch") == lead.data.get("silence", {}).get(
        "epoch"
    ):
        schedule(db, lead, row.payload["touch_reason"])


def step(db):
    from ama.models import Lead

    t = db.scalar(
        select(LeadTouch)
        .where(LeadTouch.status == "pending", LeadTouch.due_at <= now())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if not t:
        return False
    lead = db.scalar(select(Lead).where(Lead.id == t.lead_id).with_for_update())
    silence = lead.data.get("silence", {}) if lead else {}
    if not lead or not allowed(db, lead, t.reason) or silence.get("epoch") != t.epoch or silence.get("sent", 0) >= 2:
        t.status = "cancelled"
        return True
    contact = lead.data.get("contact", {})
    if not isinstance(contact, dict) or not contact.get("chat_id"):
        t.status = "blocked"
        return True
    try:
        cal = configured_calendar(db)
        if not cal:
            raise BusinessError("Нет календаря продаж.")
    except BusinessError:
        t.status = "blocked"
        return True
    permitted = cal.add_minutes(now(), 0)
    if permitted > now():
        t.due_at = permitted
        return True
    text = template(db, lead, t.reason, t.ordinal)
    if not text:
        t.status = "blocked"
        return True
    db.add(
        Outbox(
            dedupe_key=f"touch:{t.id}:{t.version}",
            platform=contact["platform"],
            chat_id=contact["chat_id"],
            payload={
                "text": text.data["text"],
                "lead_id": lead.id,
                "lead_touch_id": t.id,
                "silence_epoch": t.epoch,
                "touch_version": t.version,
                "template_id": text.id,
                "template_version": text.version,
            },
        )
    )
    t.status = "queued"
    return True
