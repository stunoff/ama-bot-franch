"""Versioned recurring analytical reports; no replay of historical missed cycles."""

from calendar import monthrange
from datetime import timedelta, timezone
from copy import deepcopy
from sqlalchemy import select
from ama.models import Record, ReportPlan, Binding, now
from ama.auth import Actor
from ama.config import settings
from ama.errors import BusinessError
from ama.service import parse_date
from ama.calendar import Calendar
from ama.ui import reports, access

REPORTS = {"works", "summary", "training", "sales_events", "sales_funnel", "sales_cohort"}


def validate(db, data, owner_id=None):
    if data.get("event") != "report":
        return
    if (
        data.get("report") not in REPORTS
        or data.get("frequency") not in {"daily", "weekly", "monthly"}
        or data.get("period") not in {"previous_day", "previous_week", "previous_month", "last_days"}
    ):
        raise BusinessError("Выберите отчёт, период и поддерживаемую периодичность.")
    if not data.get("approval") or not data.get("audience_approval"):
        raise BusinessError("Нужно утверждение данных и доступа всех участников чата получателя.")
    start, end = parse_date(data["start_at"]), parse_date(data["until_at"])
    if end <= start:
        raise BusinessError("Нужна конечная граница календаря позже первого запуска.")
    if data["period"] == "last_days" and (
        not isinstance(data.get("days"), int) or isinstance(data["days"], bool) or not 1 <= data["days"] <= 366
    ):
        raise BusinessError("Укажите период отчёта 1–366 дней.")
    calendar = db.scalar(
        select(Record).where(Record.kind == "calendar", Record.key == data.get("calendar"), Record.published.is_(True))
    )
    if not calendar:
        raise BusinessError("Нужен опубликованный рабочий календарь отчёта.")
    Calendar(calendar.data)
    b = db.get(Binding, data.get("binding_id"))
    if not b or b.bot_id != settings().bot_id or not b.active or not b.verified or b.purpose == "client":
        raise BusinessError("Отчёт отправляется в проверенный внутренний рабочий маршрут.")
    if owner_id:
        actor = Actor(owner_id, b.id)
        access.require_staff(db, actor)
        # Both permissions and report-specific scope are exercised before publication.
        probe = {
            **{k: data.get(k) for k in ("partner_id", "brand_id", "establishment_id")},
            "report": data["report"],
            "start": start.date().isoformat(),
            "end": start.date().isoformat(),
        }
        reports.rows(db, actor, probe)


def on_record(db, rec):
    if rec.kind != "policy":
        return
    plan = db.scalar(select(ReportPlan).where(ReportPlan.record_id == rec.id).with_for_update())
    if rec.data.get("policy_type") != "schedule" or rec.data.get("event") != "report" or not rec.published:
        if plan:
            plan.enabled = False
        return
    validate(db, rec.data, rec.owner_id)
    if not rec.owner_id:
        raise BusinessError("Назначьте владельца регулярного отчёта.")
    b = db.get(Binding, rec.data["binding_id"])
    data = {
        "snapshot": deepcopy(rec.data),
        "owner_id": rec.owner_id,
        "binding_version": b.version,
        "grants": access.grants_hash(db, Actor(rec.owner_id, b.id)),
        "scheduled_at": parse_date(rec.data["start_at"]).isoformat() + "Z",
        "calendar": deepcopy(
            db.scalar(select(Record).where(Record.kind == "calendar", Record.key == rec.data["calendar"])).data
        ),
    }
    if not plan:
        plan = ReportPlan(
            record_id=rec.id,
            record_version=rec.version,
            bot_id=settings().bot_id,
            next_at=parse_date(rec.data["start_at"]),
            data=data,
        )
        db.add(plan)
    else:
        plan.record_version, plan.enabled, plan.next_at, plan.data = (
            rec.version,
            True,
            parse_date(rec.data["start_at"]),
            data,
        )


def next_due(slot, data, cal):
    local = slot.replace(tzinfo=timezone.utc).astimezone(cal.tz)
    if data["frequency"] == "monthly":
        year, month = local.year + (local.month == 12), local.month % 12 + 1
        anchor = parse_date(data["start_at"]).replace(tzinfo=timezone.utc).astimezone(cal.tz)
        local = local.replace(year=year, month=month, day=min(anchor.day, monthrange(year, month)[1]))
    else:
        local += timedelta(days=1 if data["frequency"] == "daily" else 7)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def period(slot, data, cal):
    day = slot.replace(tzinfo=timezone.utc).astimezone(cal.tz).date()
    if data["period"] == "previous_month":
        end = day.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
    elif data["period"] == "previous_week":
        end = day - timedelta(days=day.weekday() + 1)
        start = end - timedelta(days=6)
    else:
        end = day - timedelta(days=1)
        start = end - timedelta(days=data.get("days", 1) - 1 if data["period"] == "last_days" else 0)
    return start.isoformat(), end.isoformat()


def tick(db):
    plan = db.scalar(
        select(ReportPlan)
        .where(ReportPlan.bot_id == settings().bot_id, ReportPlan.enabled.is_(True), ReportPlan.next_at <= now())
        .order_by(ReportPlan.next_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if not plan:
        return False
    rec = db.get(Record, plan.record_id)
    data = plan.data["snapshot"]
    if not rec or not rec.published or rec.version != plan.record_version:
        plan.enabled = False
        return True
    b = db.get(Binding, data["binding_id"])
    actor = Actor(plan.data["owner_id"], data["binding_id"])
    try:
        if (
            not b
            or b.version != plan.data["binding_version"]
            or not b.active
            or not b.verified
            or access.grants_hash(db, actor) != plan.data["grants"]
        ):
            raise BusinessError("Права / маршрут отчёта изменились; нужна перепроверка владельцем.")
        cal = Calendar(plan.data["calendar"])
        slot = parse_date(plan.data["scheduled_at"])
        end = parse_date(data["until_at"])
        if slot > end:
            plan.enabled = False
            return True
        following = next_due(slot, data, cal)
        skipped = 0
        while following <= now() and following <= end:
            slot, following = following, next_due(following, data, cal)
            skipped += 1
            if skipped > 10000:
                raise BusinessError("Слишком большой пропущенный период; требуется новое расписание.")
        allowed = cal.add_minutes(now(), 0)
        if allowed > now():
            plan.next_at = allowed
            return True
        start, finish = period(slot, data, cal)
        args = {
            "timezone": cal.tz.key,
            "report": data["report"],
            "start": start,
            "end": finish,
            **{k: data.get(k) for k in ("partner_id", "brand_id", "establishment_id")},
        }
        key = "scheduled-report:" + plan.id + ":" + str(rec.version) + ":" + slot.isoformat()
        # A unique export delivery key makes a repeated tick safe.
        from ama.models import Outbox

        existing = db.scalar(select(Outbox).where(Outbox.dedupe_key == "export:" + key))
        if not existing:
            result = reports.create_export(db, actor, args, key)
            existing = db.scalar(select(Outbox).where(Outbox.payload["export_id"].as_string() == result["id"]))
            existing.payload = {
                **existing.payload,
                "report_plan_id": plan.id,
                "report_plan_version": plan.record_version,
            }
        plan.data = {
            **plan.data,
            "scheduled_at": following.isoformat() + "Z",
            "last_period": {"start": start, "end": finish},
            "missed_cycles": plan.data.get("missed_cycles", 0) + skipped,
            "last_outbox": existing.id,
        }
        plan.next_at = following
        plan.enabled = following <= end
    except BusinessError as error:
        plan.enabled = False
        plan.data = {**plan.data, "blocked": str(error)}
    return True


def deliverable(db, row):
    ident = row.payload.get("report_plan_id")
    if not ident:
        return True
    plan = db.get(ReportPlan, ident)
    rec = db.get(Record, plan.record_id) if plan else None
    return bool(
        rec
        and rec.published
        and rec.version == row.payload["report_plan_version"]
        and plan.record_version == rec.version
    )
