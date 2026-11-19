from datetime import datetime, timedelta
import pytest
from sqlalchemy import select, func
from ama.auth import Actor, identify
from ama.calendar import Calendar
from ama.errors import BusinessError, AccessDenied, Conflict
from ama.models import (Work, Attachment, Binding, Training, Role, Record, Lead, Deal, Payment,
                        Outbox, now)
from ama.service import transition, attach, training_action, create_work
from ama import admin, sales, broadcasts, projects, operations


def act(c, actor, action, data=None, version=None, key=None):
    return transition(c.db, actor, c.w.id, version if version is not None else c.w.version,
                      action, data or {}, key or f"{action}:{c.w.version}")


def test_submission_is_not_acceptance(ctx):
    act(ctx, ctx.fp, "take")
    act(ctx, ctx.fp, "submit")
    assert ctx.w.status == "review" and ctx.w.outcome is None and ctx.w.owner_id == "curator"
    act(ctx, ctx.curator, "accept")
    assert ctx.w.status == "done" and ctx.w.outcome == "accepted"


def test_admin_has_no_business_power(ctx):
    with pytest.raises(AccessDenied):
        act(ctx, ctx.admin, "take")


def test_observer_cannot_submit(ctx):
    with pytest.raises(AccessDenied):
        act(ctx, ctx.observer, "take")


def test_old_button_and_repeat(ctx):
    act(ctx, ctx.fp, "take", key="same-command", version=1)
    version = ctx.w.version
    act(ctx, ctx.fp, "take", key="same-command", version=1)
    assert ctx.w.version == version
    with pytest.raises(Conflict):
        act(ctx, ctx.fp, "submit", version=1, key="different")


def test_cross_chat_denied(ctx):
    with pytest.raises(AccessDenied):
        act(ctx, Actor("fp", "new"), "take")


def test_expired_role_denied(ctx):
    r = ctx.db.scalar(select(Role).where(Role.person_id == "fp"))
    r.expires_at = now() - timedelta(seconds=1)
    ctx.db.flush()
    with pytest.raises(AccessDenied):
        act(ctx, ctx.fp, "take")


def test_attachment_does_not_submit_and_redelivery(ctx):
    a = attach(ctx.db, ctx.observer, "work", "telegram", "file123", "photo.jpg", "unique-file", "album")
    b = attach(ctx.db, ctx.observer, "work", "telegram", "file123", "photo.jpg", "unique-file", "album")
    assert a.id == b.id and a.author_id == "observer" and ctx.w.status == "new"
    assert ctx.db.scalar(select(func.count()).select_from(Attachment)) == 1


def test_pending_file_blocks_submission(ctx):
    act(ctx, ctx.fp, "take")
    attach(ctx.db, ctx.fp, "work", "telegram", "file123", "photo.jpg", "file")
    with pytest.raises(BusinessError, match="сохранены"):
        act(ctx, ctx.fp, "submit")


def test_rejection_keeps_same_item(ctx):
    act(ctx, ctx.fp, "take")
    act(ctx, ctx.fp, "submit")
    act(ctx, ctx.curator, "reject", {"reason": "Нужен другой ракурс", "due_at": "2030-01-01T12:00:00+03:00"})
    assert ctx.w.id == "work" and ctx.w.status == "rework" and ctx.w.returns == 1
    assert ctx.w.owner_id == "fp"


def test_handoff_owner_retained_until_accept(ctx):
    ctx.w.owner_id = ctx.w.executor_id = "curator"
    act(ctx, ctx.curator, "handoff", {"person_id": "specialist", "binding_id": "new"})
    assert ctx.w.owner_id == "curator" and ctx.w.pending_owner_id == "specialist"
    act(ctx, Actor("specialist", "new"), "handoff_accept")
    assert ctx.w.owner_id == "specialist" and ctx.w.binding_id == "new"
    assert ctx.w.original_due_at > now()


def test_wait_requires_control_point(ctx):
    ctx.w.owner_id = "curator"
    with pytest.raises(BusinessError):
        act(ctx, ctx.curator, "wait_external", {"party": "Банк"})


def test_deadline_never_erases_original(ctx):
    ctx.w.owner_id = "curator"
    original = ctx.w.original_due_at
    act(ctx, ctx.curator, "wait_external", {"party": "Банк", "required": "Ответ", "next_check": "2030-01-01T12:00:00+03:00"})
    assert ctx.w.original_due_at == original


def test_training_is_personal_and_resumable(ctx):
    t = training_action(ctx.db, ctx.fp, "general", "open")
    t = training_action(ctx.db, ctx.fp, "general", "next", t.version)
    assert t.position == 1 and not t.completed_at
    t = training_action(ctx.db, ctx.fp, "general", "later", t.version)
    assert t.paused
    t = training_action(ctx.db, ctx.fp, "general", "next", t.version)
    assert t.completed_at
    t = training_action(ctx.db, ctx.fp, "general", "back", t.version)
    assert t.completed_at and t.position == 0
    assert ctx.db.scalar(select(func.count()).select_from(Training)) == 1


def test_training_missing_material_preserves_progress(ctx):
    course = ctx.db.scalar(select(Record).where(Record.kind == "course"))
    course.data = {"cards": [{"title": "T", "text": "T", "required_materials": ["missing"]}]}
    ctx.db.flush()
    t = training_action(ctx.db, ctx.fp, "general", "open")
    with pytest.raises(BusinessError):
        training_action(ctx.db, ctx.fp, "general", "next", t.version)
    assert t.position == 0 and not t.acknowledged and not t.completed_at


def test_calendar_friday_holiday():
    cal = Calendar({"timezone": "Europe/Moscow", "weekdays": [0, 1, 2, 3, 4], "start": "11:00", "end": "20:00", "holidays": ["2026-11-16"]})
    # Пятница 19:30 МСК + 1 рабочий час -> вторник 11:30 МСК.
    assert cal.add_minutes(datetime(2026, 11, 13, 16, 30), 60) == datetime(2026, 11, 17, 8, 30)


def test_last_admin_cannot_be_removed(ctx):
    r = ctx.db.scalar(select(Role).where(Role.name == "admin"))
    with pytest.raises(BusinessError, match="последнего"):
        admin.apply(ctx.db, ctx.admin, {"type": "revoke_role", "data": {"id": r.id}})


def test_settings_only_in_management(ctx):
    with pytest.raises(AccessDenied):
        admin.inspect(ctx.db, Actor("admin", "support"))


def test_record_optimistic_lock(ctx):
    d = {"kind": "template", "key": "hello", "value": {"text": "one"}}
    admin.apply(ctx.db, ctx.admin, {"type": "record", "data": d})
    admin.apply(ctx.db, ctx.admin, {"type": "record", "data": {**d, "value": {"text": "two"}, "version": 1}})
    with pytest.raises(Conflict):
        admin.apply(ctx.db, ctx.admin, {"type": "record", "data": {**d, "version": 1}})


def test_unverified_route_cannot_activate(ctx):
    b = ctx.db.get(Binding, "new")
    b.verified = False
    with pytest.raises(BusinessError):
        admin.apply(ctx.db, ctx.admin, {"type": "activate_binding", "data": {"id": "new", "version": 1}})


def test_client_request_uses_configured_owner(ctx):
    w = create_work(ctx.db, ctx.fp, {"title": "Помогите", "kind": "support", "owner_id": "admin", "reviewer_id": "admin"}, "request1")
    assert w.owner_id == "curator" and w.reviewer_id == "curator" and w.partner_id == "partner"
    assert create_work(ctx.db, ctx.fp, {"title": "Дубль"}, "request1").id == w.id


def test_existing_scenario_creates_no_fictional_opening(ctx):
    result = projects.configure(ctx.db, ctx.curator, {"partner_id": "partner", "establishment_id": "est", "scenario": "existing", "confirmed": True, "project_id": "p1", "blocks": []}, "project")
    assert result["works"] == []
    with pytest.raises(BusinessError):
        projects.configure(ctx.db, ctx.curator, {"partner_id": "partner", "establishment_id": "est", "scenario": "existing", "confirmed": True,
                           "project_id": "p1", "blocks": [{"key": "technical_opening", "mode": "perform"}]}, "project")


def test_refusal_survives_reentry(ctx):
    ctx.db.add(Record(kind="consent", key="v1", data={"text": "Согласие"}, published=True))
    lead = sales.lead_entry(ctx.db, "other", "bot", "v1")
    lead.owner_id = "consultant"
    sales.update_lead(ctx.db, ctx.consultant, lead.id, lead.version, "refuse", {"reason": "Не интересно"})
    sales.lead_entry(ctx.db, "other", "bot", "v1")
    assert lead.no_contact and lead.result == "refused"


def test_contract_requires_current_signature_and_verified_money(ctx):
    lead = Lead(person_id="other", source="bot", consent_at=now(), consent_version="v1", owner_id="consultant", data={})
    ctx.db.add(lead)
    ctx.db.flush()
    deal = Deal(lead_id=lead.id, required_minor=10000, data={})
    ctx.db.add(deal)
    ctx.db.flush()
    sales.contract_action(ctx.db, ctx.contract, deal.id, deal.version, "payment", {"amount_minor": 10000, "bank_reference": "bank1"}, "pay")
    with pytest.raises(BusinessError):
        sales.contract_action(ctx.db, ctx.contract, deal.id, deal.version, "confirm", {}, "confirm")
    sales.contract_action(ctx.db, ctx.contract, deal.id, deal.version, "signed", {"evidence": "ЭДО"}, "sign")
    sales.contract_action(ctx.db, ctx.contract, deal.id, deal.version, "package", {"document_reference": "v2"}, "package")
    with pytest.raises(BusinessError):
        sales.contract_action(ctx.db, ctx.contract, deal.id, deal.version, "confirm", {}, "confirm2")
    sales.contract_action(ctx.db, ctx.contract, deal.id, deal.version, "signed", {"evidence": "ЭДО v2"}, "sign2")
    sales.contract_action(ctx.db, ctx.contract, deal.id, deal.version, "confirm", {}, "confirm3")
    assert deal.successful_at and lead.result == "success" and not deal.handoff_accepted_at
    assert ctx.db.scalar(select(func.count()).select_from(Payment)) == 1


def test_admin_cannot_confirm_payment(ctx):
    with pytest.raises(AccessDenied):
        sales.contract_action(ctx.db, ctx.admin, "deal", 1, "payment", {}, "key")


def test_cohort_does_not_include_old_success(ctx):
    for i, created in enumerate([datetime(2026, 9, 1), datetime(2026, 10, 3), datetime(2026, 10, 4)]):
        lead = Lead(person_id=["other", "fp", "observer"][i], source="test", consent_at=now(), consent_version="v1", created_at=created)
        ctx.db.add(lead)
        ctx.db.flush()
        if i == 0:
            ctx.db.add(Deal(lead_id=lead.id, required_minor=1, successful_at=datetime(2026, 10, 10)))
    ctx.db.flush()
    result = sales.cohort_report(ctx.db, datetime(2026, 10, 1), datetime(2026, 11, 1))
    assert result == {"cohort_leads": 2, "cohort_won": 0, "period_wins": 1, "cohort_conversion": 0}


def test_broadcast_one_message_per_brand_chat(ctx):
    ctx.marketing=Actor("marketing","marketing")
    ctx.db.add(Record(kind="policy",key="permission-marketing",published=True,data={"policy_type":"broadcast_permission","person_id":"marketing","brand_ids":["brand"],"allow_general":False,"approval":"Назначено администратором"}))
    partner = ctx.db.get(__import__('ama.models', fromlist=['Entity']).Entity, "partner")
    partner.data = {"notification_window": {"timezone": "Europe/Moscow", "weekdays": [0, 1, 2, 3, 4], "start": "11:00", "end": "20:00"}}
    ctx.db.flush()
    b = broadcasts.preview(ctx.db, ctx.marketing, "Новость", "brand")
    broadcasts.publish(ctx.db, ctx.marketing, b.id, b.version)
    broadcasts.publish(ctx.db, ctx.marketing, b.id, b.version)
    ctx.db.flush()
    assert ctx.db.scalar(select(func.count()).select_from(Outbox).where(Outbox.dedupe_key.like("broadcast:%"))) == 1


def test_paid_work_starts_by_approved_partner_rule(ctx):
    ctx.w.kind, ctx.w.owner_id, ctx.w.executor_id = "paid_work", "curator", "curator"
    ctx.w.binding_id = "design"
    operations.operation(ctx.db, Actor("curator", "design"), "work", ctx.w.version, "offer", {
        "scope": "Дизайн", "amount_minor": 5000, "valid_until": "2030-01-01T12:00:00+03:00",
        "deadline": "2030-02-01T12:00:00+03:00", "start_rule": "partner_payment_evidence"})
    with pytest.raises(BusinessError):
        operations.operation(ctx.db, ctx.fp, "work", ctx.w.version, "offer_accept", {"offer_version": 1, "amount_minor": 4000, "evidence": "Скрин"})
    operations.operation(ctx.db, ctx.fp, "work", ctx.w.version, "offer_accept", {"offer_version": 1, "amount_minor": 5000, "evidence": "Подтверждение"})
    assert ctx.w.status == "in_progress" and ctx.w.data["paid_started_at"]


def test_care_required_without_call_center(ctx):
    ctx.w.kind, ctx.w.executor_id, ctx.w.owner_id = "digital", "specialist", "specialist"
    operations.operation(ctx.db, ctx.specialist, "work", ctx.w.version, "passport", {"call_center_applicable": False, "systems": ["iiko"],
                         "work_phone_messenger": "max", "management_messenger": "telegram"})
    assert "care" in ctx.w.data["required_components"] and "call_center" not in ctx.w.data["required_components"]
    with pytest.raises(BusinessError, match="цифровые"):
        ctx.w.status = "in_progress"
        act(ctx, ctx.specialist, "submit")


def test_opening_requires_personal_training(ctx):
    ctx.w.kind = "internal_opening"
    with pytest.raises(BusinessError, match="ознакомления"):
        operations.operation(ctx.db, ctx.curator, "work", ctx.w.version, "opening_confirm", {"evidence": "Проверено"})


def test_chat_migration_keeps_work_and_deadline(ctx):
    old_due = ctx.w.due_at
    admin.apply(ctx.db, ctx.admin, {"type": "replace_binding", "data": {"old_id": "support", "new_id": "new", "version": 1}})
    assert ctx.w.binding_id == "new" and ctx.w.due_at == old_due and ctx.w.id == "work"
    with pytest.raises(AccessDenied):
        identify(ctx.db, "telegram", "curator", "support")


def test_request_wizard_preserves_scope_and_rejects_old_button(ctx):
    from ama import wizard
    from ama.models import Dialog
    wizard.start(ctx.db, ctx.fp)
    f = ctx.db.scalar(select(Dialog))
    first_version = f.version
    wizard.advance(ctx.db, ctx.fp, f.id, f.version, 0, "one")  # По бренду в целом, нет связанного объекта.
    with pytest.raises(Conflict):
        wizard.advance(ctx.db, ctx.fp, f.id, first_version, 0, "old")
    wizard.advance(ctx.db, ctx.fp, f.id, f.version, 1, "branch")  # Другой вопрос.
    wizard.advance(ctx.db, ctx.fp, f.id, f.version, 0, "category")
    while f.stage == "question":
        wizard.advance(ctx.db, ctx.fp, f.id, f.version, 0, f"question:{f.version}")
    assert f.stage == "preview"
    wizard.advance(ctx.db, ctx.fp, f.id, f.version, 0, "confirm")
    assert f.stage == "done"
    w = ctx.db.get(Work, f.data["work_id"])
    assert w.partner_id == "partner" and w.brand_id == "brand" and w.establishment_id is None


def test_wizard_cannot_be_completed_by_other_person(ctx):
    from ama import wizard
    from ama.models import Dialog
    wizard.start(ctx.db, ctx.fp)
    f = ctx.db.scalar(select(Dialog))
    with pytest.raises(AccessDenied):
        wizard.advance(ctx.db, ctx.observer, f.id, f.version, 0, "foreign")


def test_internal_attachment_keeps_visibility(ctx):
    a = attach(ctx.db, ctx.curator, "work", "telegram", "file", "document.pdf", "internal")
    b = attach(ctx.db, ctx.fp, "work", "telegram", "photo", "photo.jpg", "client")
    assert a.visibility == "internal" and b.visibility == "client"


def test_paid_offer_forbidden_in_support_queue(ctx):
    ctx.w.kind = "paid_work"
    with pytest.raises(BusinessError, match="не разрешены"):
        operations.operation(ctx.db, ctx.curator, "work", ctx.w.version, "offer", {"scope": "Тест"})


def test_lead_reminder_limit_is_not_reset_by_bot(ctx):
    from ama import reminders
    from ama.models import LeadTouch
    calendar = ctx.db.scalar(select(Record).where(Record.kind == "calendar", Record.key == "support"))
    ctx.db.add(Record(kind="calendar", key="sales", data=calendar.data, published=True))
    for n in (1, 2):
        ctx.db.add(Record(kind="template", key=f"lead.consultant_reply.{n}", data={"text": "Удобно продолжить?"}, published=True))
    lead = Lead(person_id="other", source="bot", consent_at=now(), consent_version="v1", data={})
    ctx.db.add(lead)
    ctx.db.flush()
    reminders.react(ctx.db, lead, {"platform": "telegram", "chat_id": "other"})
    t = reminders.schedule(ctx.db, lead, "consultant_reply")
    ctx.db.flush()
    assert reminders.schedule(ctx.db, lead, "consultant_reply").id == t.id
    t.status = "queued"
    msg = Outbox(dedupe_key="touch1", platform="telegram", chat_id="other", payload={"lead_id": lead.id, "lead_touch_id": t.id})
    reminders.on_delivered(ctx.db, msg)
    ctx.db.flush()
    second = ctx.db.scalar(select(LeadTouch).where(LeadTouch.ordinal == 2))
    assert second and lead.data["silence"]["sent"] == 1
    second.status = "queued"
    msg.payload = {"lead_id": lead.id, "lead_touch_id": second.id}
    reminders.on_delivered(ctx.db, msg)
    assert reminders.schedule(ctx.db, lead, "materials") is None
    assert lead.data["silence"]["sent"] == 2


def test_lead_reply_cancels_queued_reminder(ctx):
    from ama import reminders
    from ama.models import LeadTouch
    lead = Lead(person_id="other", source="bot", consent_at=now(), consent_version="v1", data={"silence": {"epoch": "old", "sent": 1}})
    ctx.db.add(lead)
    ctx.db.flush()
    touch = LeadTouch(lead_id=lead.id, epoch="old", ordinal=2, reason="materials", due_at=now(), status="queued")
    ctx.db.add(touch)
    ctx.db.flush()
    msg = Outbox(dedupe_key="queued", platform="telegram", chat_id="other", payload={"lead_id": lead.id, "lead_touch_id": touch.id})
    ctx.db.add(msg)
    ctx.db.flush()
    reminders.react(ctx.db, lead)
    assert touch.status == "cancelled" and msg.status == "cancelled" and lead.data["silence"]["sent"] == 0
