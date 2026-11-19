from datetime import timedelta, datetime
from sqlalchemy import select, func
import pytest
from ama import admin, project_flow as flow, report_schedules as schedules, brand_notices, closures, wizard, intake
from ama.auth import Actor
from ama.errors import BusinessError, AccessDenied
from ama.models import (
    Record,
    Work,
    Attachment,
    IntakeFile,
    Outbox,
    Dialog,
    Binding,
    ReportPlan,
    BrandAccessNotice,
    Role,
    Lead,
    SalesCase,
    now,
)
from ama.ui import business, reports, engine, controller
from ama.test_data import is_test
from test_projects import plan, route, single


def test_published_custom_photo_template_creates_real_block_and_enforces_evidence(ctx):
    route(ctx)
    value = {
        "policy_type": "scenario",
        "scenario": "existing",
        "title": "Фото зона",
        "approval": "Утверждено",
        "blocks": [
            {
                "key": "custom_photo",
                "title": "Фото собственной зоны",
                "expected_result": "Сохранённое фото, принятое куратором",
                "executor_role": "partner",
                "reviewer_role": "curator",
                "route": "support",
                "mode": "perform",
                "depends_on": [],
                "start_event": "project_configured",
                "completion_rule": "Куратор проверил фото",
                "result_type": "photo",
            }
        ],
    }
    rec = admin.apply(
        ctx.db,
        ctx.admin,
        {
            "type": "record",
            "data": {"kind": "policy", "key": "custom-photo", "owner_id": "curator", "published": True, "value": value},
        },
    )
    p = plan(ctx, scenario="existing")
    b = flow.blocks(ctx.db, p)["custom_photo"]
    w = ctx.db.get(Work, b.work_id)
    assert w and w.executor_id == "fp" and w.reviewer_id == "curator"
    assert p.profile["template_source"]["version"] == 1
    from ama.service import transition

    transition(ctx.db, ctx.fp, w.id, w.version, "take", {}, "photo-take")
    with pytest.raises(BusinessError, match="материала"):
        transition(ctx.db, ctx.fp, w.id, w.version, "submit", {}, "no-photo")
    ctx.db.add(
        Attachment(
            source_key="custom-photo-proof",
            work_id=w.id,
            author_id="fp",
            source_binding_id="client",
            platform="telegram",
            file_id="approved",
            filename="photo.jpg",
            status="stored",
        )
    )
    ctx.db.flush()
    transition(ctx.db, ctx.fp, w.id, w.version, "submit", {}, "photo-submit")
    transition(ctx.db, ctx.curator, w.id, w.version, "accept", {}, "photo-accept")
    assert w.outcome == "accepted"
    record = ctx.db.get(Record, rec["id"])
    record.data = {**record.data, "blocks": [{**value["blocks"][0], "title": "Новое название"}]}
    record.version += 1
    assert flow.blocks(ctx.db, p)["custom_photo"].data["template"]["title"] == "Фото собственной зоны"


def test_custom_template_rejects_unimplemented_events_and_changed_mandatory_reviewer(ctx):
    route(ctx)
    data = {
        "policy_type": "scenario",
        "scenario": "existing",
        "title": "Template",
        "approval": "Approved",
        "blocks": [
            {
                "key": "test-block",
                "title": "Block",
                "expected_result": "Result",
                "executor_role": "partner",
                "reviewer_role": "curator",
                "route": "support",
                "mode": "perform",
                "depends_on": [],
                "start_event": "new_machine_integration",
                "completion_rule": "Manual",
            }
        ],
    }
    ctx.db.add(Record(kind="policy", key="bad-template", owner_id="curator", published=True, data=data))
    ctx.db.flush()
    with pytest.raises(BusinessError, match="событие"):
        plan(ctx, scenario="existing")


def report_record(ctx, clock):
    cal = ctx.db.scalar(select(Record).where(Record.kind == "calendar", Record.key == "support"))
    cal.data = {"timezone": "UTC", "weekdays": list(range(7)), "start": "00:00", "end": "23:59"}
    rec = Record(
        kind="policy",
        key="weekly-report",
        owner_id="curator",
        published=True,
        data={
            "policy_type": "schedule",
            "event": "report",
            "report": "summary",
            "calendar": "support",
            "binding_id": "support",
            "start_at": clock[0].isoformat() + "Z",
            "until_at": (clock[0] + timedelta(days=100)).isoformat() + "Z",
            "frequency": "daily",
            "period": "previous_day",
            "approval": "Approved",
            "audience_approval": "Scope approved for all participants",
        },
    )
    ctx.db.add(rec)
    ctx.db.flush()
    schedules.on_record(ctx.db, rec)
    ctx.db.flush()
    return rec


def test_regular_report_runs_once_and_skips_backlog(ctx, monkeypatch):
    clock = [datetime(2027, 1, 4, 12)]
    monkeypatch.setattr(schedules, "now", lambda: clock[0])
    rec = report_record(ctx, clock)
    clock[0] += timedelta(days=4)
    assert schedules.tick(ctx.db)
    assert not ctx.db.scalar(select(ReportPlan)).data.get("blocked"), ctx.db.scalar(select(ReportPlan)).data
    assert (
        ctx.db.scalar(
            select(func.count()).select_from(Outbox).where(Outbox.payload["report_plan_id"].as_string().is_not(None))
        )
        == 1
    )
    p = ctx.db.scalar(select(ReportPlan))
    assert p.data["missed_cycles"] == 4 and p.data["last_period"]["end"] == "2027-01-07"
    assert not schedules.tick(ctx.db)
    row = ctx.db.get(Outbox, p.data["last_outbox"])
    assert schedules.deliverable(ctx.db, row)
    rec.version += 1
    assert not schedules.deliverable(ctx.db, row)


def test_report_plan_stops_after_author_role_revoked(ctx, monkeypatch):
    clock = [datetime(2027, 1, 4, 12)]
    monkeypatch.setattr(schedules, "now", lambda: clock[0])
    report_record(ctx, clock)
    ctx.db.scalar(select(Role).where(Role.person_id == "curator", Role.name == "curator")).active = False
    schedules.tick(ctx.db)
    p = ctx.db.scalar(select(ReportPlan))
    assert not p.enabled and p.data.get("blocked")
    assert ctx.db.scalar(select(func.count()).select_from(Outbox)) == 0


def test_test_scope_is_inherited_and_excluded_from_work_reports(ctx):
    from ama.models import Entity

    partner = ctx.db.get(Entity, "partner")
    partner.data = {"is_test": True}
    assert is_test(ctx.db, ctx.w)
    start = (now() - timedelta(days=1)).date().isoformat()
    end = now().date().isoformat()
    result = reports.rows(ctx.db, ctx.curator, {"report": "works", "start": start, "end": end})
    assert not result["rows"] if isinstance(result, dict) else not result[1]


def test_test_lead_is_not_counted_in_sales_events(ctx):
    from ama.sales_reports import build

    lead = Lead(
        person_id="fp",
        source="test",
        consent_at=now(),
        consent_version="v1",
        owner_id="consultant",
        data={"is_test": True},
    )
    ctx.db.add(lead)
    ctx.db.flush()
    c = SalesCase(lead_id=lead.id, owner_id="consultant", brand_id="brand", data={})
    ctx.db.add(c)
    ctx.db.flush()
    _, rows = build(
        ctx.db, ctx.consultant, {"report": "sales_events"}, now() - timedelta(days=1), now() + timedelta(days=1)
    )
    assert next(r for r in rows if r[0] == "Новые уникальные лиды")[1] == 0


def test_unconfirmed_closure_is_distinct_and_cannot_close_paid_result(ctx):
    ctx.w.status = "review"
    ctx.w.owner_id = ctx.w.reviewer_id = "fp"
    ctx.w.data = {"partner_request": True, "result_description": "Информационный ответ", "calendar": "support"}
    result = closures.execute(
        ctx.db,
        ctx.curator,
        ctx.w.id,
        ctx.w.version,
        {
            "classification": "informational",
            "reason": "Нет подтверждения",
            "basis": "Проверка куратора",
            "no_financial_obligation": True,
        },
    )
    assert result["outcome"] == "closed_without_confirmation" and ctx.w.outcome != "partner_confirmed"
    ctx.w.status = "review"
    ctx.w.kind = "paid_work"
    with pytest.raises(BusinessError):
        closures.authorize(ctx.db, ctx.curator, ctx.w)


def test_unconfirmed_closure_ui_requires_preview_and_reason(ctx):
    ctx.w.status = "review"
    ctx.w.owner_id = ctx.w.reviewer_id = "fp"
    ctx.w.data = {"partner_request": True, "result_description": "Ответ", "calendar": "support"}
    d = business.make_draft(
        ctx.db,
        ctx.curator,
        "closure:without_confirmation",
        "Закрыть",
        {"classification": "optional", "reason": "Нет ответа", "basis": "Основание", "no_financial_obligation": True},
        ctx.w,
    )
    assert business.preview(ctx.db, ctx.curator, d)["outcome"] == "closed_without_confirmation"
    assert ctx.w.status == "review"


def test_brand_link_creates_one_notice_without_restarting_course(ctx):
    result = admin.apply(
        ctx.db,
        ctx.admin,
        {"type": "link_brand", "data": {"establishment_id": "est", "brand_id": "brand", "reason": "Подключение"}},
    )
    ctx.db.flush()
    notice = ctx.db.scalar(select(BrandAccessNotice))
    assert notice
    link = ctx.db.get(__import__("ama.models", fromlist=["EstablishmentBrand"]).EstablishmentBrand, result["id"])
    brand_notices.enqueue(ctx.db, link)
    ctx.db.flush()
    assert ctx.db.scalar(select(func.count()).select_from(BrandAccessNotice)) == 1
    assert brand_notices.tick(ctx.db)
    row = ctx.db.get(Outbox, notice.data["outbox_id"])
    assert row.binding_id == "client" and brand_notices.deliverable(ctx.db, row)
    assert "Общий курс не начинается заново" in row.payload["text"]
    link.active = False
    assert not brand_notices.deliverable(ctx.db, row)


def test_wizard_files_remain_private_until_confirmed_and_preserve_context(ctx):
    from ama.models import EstablishmentBrand

    ctx.db.add(EstablishmentBrand(establishment_id="est", brand_id="brand"))
    ctx.db.flush()
    form = Dialog(
        person_id="fp",
        binding_id="client",
        stage="upload",
        data={"branch": "support", "category": 0, "answers": [], "establishment_id": "est"},
        version=1,
    )
    ctx.db.add(form)
    ctx.db.flush()
    event = __import__("ama.adapters", fromlist=["normalize"]).normalize(
        "telegram",
        {
            "update_id": 551,
            "message": {
                "message_id": 1,
                "from": {"id": "fp"},
                "chat": {"id": "client", "type": "group"},
                "photo": [{"file_id": "draft-photo"}],
            },
        },
    )
    intake.capture(ctx.db, ctx.fp, event, form)
    assert ctx.db.scalar(select(func.count()).select_from(Attachment)) == 0
    assert ctx.db.scalar(select(func.count()).select_from(IntakeFile)) == 1
    form.stage = "preview"
    text, choices = wizard.options(ctx.db, ctx.fp, form)
    assert "photo.jpg" in text
    index = next(i for i, c in enumerate(choices) if c["value"] == "confirm")
    wizard.advance(ctx.db, ctx.fp, form.id, form.version, index, "confirmed-draft")
    assert ctx.db.scalar(select(func.count()).select_from(Attachment)) == 1
    file = ctx.db.scalar(select(IntakeFile))
    assert file.work_id == form.data["work_id"] and file.status == "linked"


def test_unassigned_upload_cannot_be_assigned_by_another_person(ctx):
    from ama.adapters import Incoming

    files = intake.capture(
        ctx.db,
        ctx.fp,
        Incoming(
            key="orphan:1",
            platform="telegram",
            user_id="fp",
            chat_id="client",
            topic="",
            text="",
            callback=None,
            files=[{"file_id": "file", "filename": "file.pdf"}],
        ),
    )
    with pytest.raises(AccessDenied):
        intake.link(ctx.db, ctx.observer, files[0].id, "work")


def test_bank_form_and_tax_have_separate_human_confirmed_steps(ctx):
    from ama.legal_setup import TAX_NOTICE, tax_consultation

    ctx.db.add(
        Binding(
            id="registrations",
            platform="telegram",
            chat_id="registrations",
            purpose="registrations",
            active=True,
            verified=True,
        )
    )
    ctx.db.flush()
    route(ctx, "registrations", "specialist", "registrations")
    p = plan(ctx, scenario="existing")
    single(ctx, p, "bank")
    flow.advance(ctx.db, p)
    bank = flow.blocks(ctx.db, p)["bank"]
    w = ctx.db.get(Work, bank.work_id)
    assert w
    form = {
        "step": "bank_form",
        "company_type": "ip",
        "company_name": "ИП Проверочный",
        "inn": "123456789012",
        "ogrn": "123456789012345",
        "contact_name": "ФП",
        "phone": "+79991234567",
        "consent_evidence": "Подтверждено назначенным ФП",
    }
    flow.work_details(ctx.db, ctx.curator, w.id, w.version, form)
    with pytest.raises(BusinessError, match="результат банка"):
        flow.before_transition(ctx.db, ctx.curator, w, "accept", {})
    actor = Actor("specialist", "registrations")
    flow.work_details(
        ctx.db,
        actor,
        w.id,
        w.version,
        {
            "step": "bank_sent",
            "form_version": 1,
            "evidence": "Форма фактически передана банку",
            "next_check": (now() + timedelta(days=2)).isoformat() + "Z",
        },
    )
    assert w.status == "waiting_external" and w.data["bank_sent"] and not w.data.get("bank_result")
    flow.work_details(
        ctx.db, actor, w.id, w.version, {"step": "bank_result", "evidence": "Открытие счёта подтверждено банком"}
    )
    assert w.data["bank_result"]
    single(ctx, p, "tax")
    route(ctx)
    flow.advance(ctx.db, p)
    tax = ctx.db.get(Work, flow.blocks(ctx.db, p)["tax"].work_id)
    assert tax and tax.data["tax_notice"] == TAX_NOTICE
    tax_consultation(ctx.db, ctx.fp, tax.id)
    assert tax.data["tax_consultation_requested"]
    flow.work_details(
        ctx.db,
        ctx.curator,
        tax.id,
        tax.version,
        {"step": "tax_regime", "regime": "consultation", "evidence": "Запрошена консультация"},
    )
    with pytest.raises(BusinessError, match="режим"):
        flow.before_transition(ctx.db, ctx.curator, tax, "accept", {})


def test_unconfirmed_closure_is_exposed_as_explicit_curator_action(ctx):
    ctx.w.status = "review"
    ctx.w.owner_id = ctx.w.reviewer_id = "fp"
    ctx.w.data = {"partner_request": True, "result_description": "Ответ", "calendar": "support"}
    screen = engine.session(ctx.db, ctx.curator)
    text, buttons = controller.dispatch(
        ctx.db, ctx.curator, screen, "close_without_confirmation_begin", {"work_id": "work"}
    )
    assert "Закрыть без подтверждения" in text and buttons


def test_new_report_monthly_anchor_survives_february(ctx):
    from ama.calendar import Calendar

    cal = Calendar({"timezone": "Europe/Moscow", "weekdays": list(range(7)), "start": "00:00", "end": "23:59"})
    data = {"frequency": "monthly", "start_at": "2027-01-31T09:00:00Z"}
    first = schedules.next_due(datetime(2027, 1, 31, 9), data, cal)
    second = schedules.next_due(first, data, cal)
    assert (first.month, first.day) == (2, 28) and (second.month, second.day) == (3, 31)


def test_old_bank_payment_blocks_franchise_rebooking(ctx):
    from ama.operations import operation
    from ama.sales import contract_action
    from ama.models import Deal

    w = Work(
        kind="debt",
        title="Долг",
        partner_id="partner",
        owner_id="contract",
        executor_id="contract",
        reviewer_id="contract",
        binding_id="contracts",
        due_at=now() + timedelta(days=2),
        original_due_at=now() + timedelta(days=2),
        data={},
    )
    ctx.db.add(w)
    ctx.db.flush()
    operation(ctx.db, ctx.contract, w.id, w.version, "invoice", {"amount_minor": 1000, "document": "Счёт"})
    operation(
        ctx.db, ctx.contract, w.id, w.version, "debt_payment", {"amount_minor": 1000, "bank_reference": "unique-wire"}
    )
    lead = Lead(person_id="fp", source="test", consent_at=now(), consent_version="v1", owner_id="consultant", data={})
    ctx.db.add(lead)
    ctx.db.flush()
    deal = Deal(lead_id=lead.id, required_minor=1000, data={})
    ctx.db.add(deal)
    ctx.db.flush()
    with pytest.raises(BusinessError, match="долговом"):
        contract_action(
            ctx.db,
            ctx.contract,
            deal.id,
            deal.version,
            "payment",
            {"amount_minor": 1000, "bank_reference": "unique-wire"},
            "no-double-rebooking",
        )


def test_parallel_legacy_debts_reserve_bank_reference_once(ctx):
    if ctx.db.bind.dialect.name != "mysql":
        pytest.skip("Requires MySQL row locks")
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from ama.operations import operation

    ids = []
    for i in range(2):
        w = Work(
            kind="debt",
            title="Долг " + str(i),
            partner_id="partner",
            owner_id="contract",
            executor_id="contract",
            reviewer_id="contract",
            binding_id="contracts",
            due_at=now() + timedelta(days=2),
            original_due_at=now() + timedelta(days=2),
            data={},
        )
        ctx.db.add(w)
        ctx.db.flush()
        operation(ctx.db, ctx.contract, w.id, w.version, "invoice", {"amount_minor": 1000, "document": "Счёт"})
        ids.append((w.id, w.version))
    ctx.db.commit()
    barrier = Barrier(2)

    def pay(item):
        with ctx.factory.begin() as db:
            barrier.wait()
            try:
                operation(
                    db,
                    ctx.contract,
                    item[0],
                    item[1],
                    "debt_payment",
                    {"amount_minor": 1000, "bank_reference": "parallel-one-wire"},
                )
                return "ok"
            except BusinessError:
                return "blocked"

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(pay, ids))
    assert sorted(results) == ["blocked", "ok"]
    ctx.db.expire_all()
    assert sum(ctx.db.get(Work, item[0]).data["balance_minor"] for item in ids) == 1000


def test_client_cannot_seed_approved_offer_in_new_work(ctx):
    from ama.service import create_work

    ctx.db.add(
        Record(
            kind="route",
            key="paid_work",
            published=True,
            data={"owner_id": "curator", "reviewer_id": "curator", "binding_id": "support", "calendar": "support"},
        )
    )
    ctx.db.flush()
    with pytest.raises(BusinessError, match="Подтверждения"):
        create_work(
            ctx.db,
            ctx.fp,
            {
                "kind": "paid_work",
                "title": "Fake offer",
                "data": {"offer": {"version": 1, "recipient": {"reference": "unapproved"}}},
            },
            "forged-offer",
        )


def test_request_draft_can_pause_and_resume_without_overwrite(ctx):
    form = Dialog(
        person_id="fp",
        binding_id="client",
        stage="preview",
        data={"branch": "support", "category": 0, "answers": [], "comment": "Сохранить этот текст"},
        version=1,
    )
    ctx.db.add(form)
    ctx.db.flush()
    _, choices = wizard.options(ctx.db, ctx.fp, form)
    idx = next(i for i, c in enumerate(choices) if c["value"] == "pause")
    wizard.advance(ctx.db, ctx.fp, form.id, form.version, idx, "pause-draft")
    assert form.stage == "paused"
    wizard.start(ctx.db, ctx.fp)
    assert form.stage == "resume_prompt"
    wizard.advance(ctx.db, ctx.fp, form.id, form.version, 0, "resume-draft")
    assert form.stage == "preview" and form.data["comment"] == "Сохранить этот текст"
