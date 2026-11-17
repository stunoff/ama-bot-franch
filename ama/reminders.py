"""Одна цепочка молчания на человека, максимум два доставленных напоминания (§1.0У)."""
from datetime import timedelta, datetime, timezone
from sqlalchemy import select
from ama.models import LeadTouch, Outbox, now, uid
from ama.service import record
from ama.calendar import Calendar
from ama.errors import BusinessError

RULES = {"onboarding": (("hours", 24), ("hours", 48)), "auto_answer": (("hours", 24), ("hours", 72)),
         "consultant_reply": (("workdays", 1), ("workdays", 3)), "clarification": (("workdays", 1), ("workdays", 3)),
         "call_time": (("workhours", 2), ("next_start", 1)), "materials": (("workdays", 1), ("hours", 96))}


def react(db, lead, contact=None):
    for touch in db.scalars(select(LeadTouch).where(LeadTouch.lead_id == lead.id, LeadTouch.status.in_(["pending", "queued"]))):
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


def schedule(db, lead, reason, start=None):
    if reason not in RULES or lead.no_contact or lead.stage == "contract" or lead.result in {"success", "refused"} or lead.data.get("next_contact"):
        return None
    silence = lead.data.get("silence", {})
    if not silence.get("epoch"):
        react(db, lead)
        silence = lead.data["silence"]
    sent = silence.get("sent", 0)
    if sent >= 2:
        return None
    from ama.models import Record
    calendar = db.scalar(select(Record).where(Record.kind == "calendar", Record.key == "sales", Record.published.is_(True)))
    if not calendar:
        return None  # Без утверждённых рабочих дней отправка не активируется.
    # Нет утверждённого текста — нет автоматического обещания/уведомления.
    template = db.scalar(select(Record).where(Record.kind == "template", Record.key == f"lead.{reason}.{sent+1}", Record.published.is_(True)))
    if not template or not template.data.get("text"):
        return None
    pending = db.scalar(select(LeadTouch).where(LeadTouch.lead_id == lead.id, LeadTouch.epoch == silence["epoch"], LeadTouch.ordinal == sent + 1))
    if pending:
        return pending
    t = LeadTouch(lead_id=lead.id, epoch=silence["epoch"], ordinal=sent + 1, reason=reason,
                  due_at=compute_due(Calendar(calendar.data), start or now(), RULES[reason][sent]))
    db.add(t)
    return t


def on_delivered(db, row):
    from ama.models import Lead
    lead_id = row.payload.get("lead_id")
    if not lead_id:
        return
    lead = db.scalar(select(Lead).where(Lead.id == lead_id).with_for_update())
    if not lead or lead.no_contact:
        return
    if row.payload.get("lead_touch_id"):
        t = db.get(LeadTouch, row.payload["lead_touch_id"])
        if not t or t.status != "queued":
            return
        t.status = "sent"
        silence = lead.data.get("silence", {})
        if silence.get("epoch") != t.epoch:
            return
        lead.data = {**lead.data, "silence": {**silence, "sent": max(silence.get("sent", 0), t.ordinal)}}
        if t.ordinal < 2:
            schedule(db, lead, t.reason)
        else:
            lead.state = "waiting_without_auto"
    elif row.payload.get("touch_reason") and row.payload.get("silence_epoch") == lead.data.get("silence", {}).get("epoch"):
        schedule(db, lead, row.payload["touch_reason"])


def step(db):
    from ama.models import Lead
    t = db.scalar(select(LeadTouch).where(LeadTouch.status == "pending", LeadTouch.due_at <= now()).with_for_update(skip_locked=True).limit(1))
    if not t:
        return False
    lead = db.scalar(select(Lead).where(Lead.id == t.lead_id).with_for_update())
    silence = lead.data.get("silence", {}) if lead else {}
    if (not lead or lead.no_contact or lead.result in {"success", "refused"} or lead.stage == "contract"
            or silence.get("epoch") != t.epoch or silence.get("sent", 0) >= 2 or lead.data.get("next_contact")):
        t.status = "cancelled"
        return True
    contact = lead.data.get("contact", {})
    if not contact.get("chat_id"):
        t.status = "blocked"
        return True
    cal = Calendar(record(db, "calendar", "sales").data)
    permitted = cal.add_minutes(now(), 0)
    if permitted > now():
        t.due_at = permitted
        return True
    template = record(db, "template", f"lead.{t.reason}.{t.ordinal}")
    db.add(Outbox(dedupe_key=f"touch:{t.id}", platform=contact["platform"], chat_id=contact["chat_id"],
                  payload={"text": template.data["text"], "lead_id": lead.id, "lead_touch_id": t.id, "silence_epoch": t.epoch}))
    t.status = "queued"
    return True
