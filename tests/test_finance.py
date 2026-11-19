from datetime import datetime, timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import select, func
from ama import finance as f
from ama.models import (
    FinanceAgreement,
    FinanceObligation,
    FinanceReceipt,
    FinanceAllocation,
    FinanceBatch,
    Record,
    Binding,
    Role,
    Identity,
    EstablishmentBrand,
    Outbox,
    Payment,
    Lead,
    Deal,
)
from ama.auth import Actor
from ama.errors import AccessDenied, BusinessError, Conflict
from ama.service import transition, create_work
from ama.ui import business, engine, controller

BASE = datetime(2027, 1, 4, 9)


@pytest.fixture
def fin(ctx, monkeypatch):
    db = ctx.db
    clock = [BASE]
    monkeypatch.setattr(f, "now", lambda: clock[0])
    monkeypatch.setattr("ama.service.now", lambda: clock[0])
    monkeypatch.setattr("ama.sla.now", lambda: clock[0])
    db.add_all([Identity(id="lawyer", name="Юрист"), Identity(id="director", name="Руководство")])
    db.flush()
    db.add_all([Role(person_id="lawyer", name="lawyer"), Role(person_id="director", name="leadership")])
    db.add_all(
        [
            Binding(id="legal", platform="telegram", chat_id="legal", purpose="legal", active=True, verified=True),
            Binding(
                id="leadership",
                platform="telegram",
                chat_id="leadership",
                purpose="leadership",
                active=True,
                verified=True,
            ),
        ]
    )
    db.add(EstablishmentBrand(establishment_id="est", brand_id="brand"))
    for purpose, person in [("contracts", "contract"), ("legal", "lawyer"), ("leadership", "director")]:
        db.add(
            Record(
                kind="route",
                key=purpose,
                published=True,
                owner_id="admin",
                data={"binding_id": purpose, "owner_id": person, "reviewer_id": person, "calendar": "support"},
            )
        )
    policy = Record(
        kind="policy",
        key="bank-recipient",
        published=True,
        owner_id="contract",
        data={
            "policy_type": "finance_recipient",
            "recipient_key": "bank-account-1",
            "reference": "https://example.org/approved-requisites",
            "approval": "Финансы утвердили",
        },
    )
    extra = Record(
        kind="policy",
        key="extra-bank",
        published=True,
        owner_id="contract",
        data={
            "policy_type": "payment",
            "recipient_key": "bank-account-extra",
            "reference": "https://example.org/extra-requisites",
            "approval": "Допуслуги утверждены",
            "services": ["software"],
        },
    )
    db.add_all(
        [
            policy,
            extra,
            Binding(
                id="software", platform="telegram", chat_id="software", purpose="software", active=True, verified=True
            ),
        ]
    )
    db.flush()
    s = SimpleNamespace(
        db=db,
        ctx=ctx,
        clock=clock,
        policy=policy,
        extra=extra,
        lawyer=Actor("lawyer", "legal"),
        director=Actor("director", "leadership"),
    )
    s.a = f.request(
        db,
        ctx.curator,
        {
            "partner_id": "partner",
            "brand_id": "brand",
            "establishment_id": "est",
            "party_details": "Подтверждённые реквизиты / список недостающего",
            "terms": "Согласованные условия",
            "basis": "Утверждённый документ",
            "due_at": (BASE + timedelta(days=30)).isoformat() + "Z",
        },
        "request-initial",
    )
    return s


def act(s, action, data):
    return f.agreement_action(s.db, s.ctx.contract, s.a.id, s.a.version, action, data)


def evidence(s, **kw):
    return {"evidence": "Проверенный документ", "at": s.clock[0].isoformat() + "Z", **kw}


def active(s):
    act(s, "package", {"document_reference": "https://example.org/contract-v1", "reason": "Актуальная редакция"})
    act(s, "edo_sent", evidence(s))
    for side in ["signed_uk", "signed_partner"]:
        act(s, side, evidence(s, package=s.a.data["package"]))
    act(
        s,
        "activate",
        {
            "purpose": "royalty",
            "payment_policy": s.policy.id,
            "amount_minor": 10000,
            "starts_at": (BASE - timedelta(days=30)).isoformat() + "Z",
            "payment_day": 10,
            "payment_timezone": "Europe/Moscow",
            "first_period_rule": "full",
            "activation_evidence": "Проверены сумма и сроки подписанного договора",
        },
    )
    return s.a


def obligation(s, period="2026-12", amount=10000):
    if not s.a.active:
        active(s)
    return f.obligation(
        s.db,
        s.ctx.contract,
        s.a.id,
        s.a.version,
        {
            "period": period,
            "amount_minor": amount,
            "due_at": (BASE - timedelta(days=10)).isoformat() + "Z",
            "basis": "Подписанные условия оплаты",
        },
    )


def invoice(s, o):
    return f.invoice(
        s.db,
        s.ctx.contract,
        o.id,
        o.version,
        {
            "number": "INV-" + o.period,
            "date": (BASE - timedelta(days=12)).isoformat() + "Z",
            "document": "https://example.org/invoice",
            "status": "sent",
            "sent_at": (BASE - timedelta(days=11)).isoformat() + "Z",
            "evidence": "Факт отправки проверен",
        },
    )


def receipt(s, amount=10000, key="bank-wire-1"):
    return f.receipt(
        s.db,
        s.ctx.contract,
        s.a.id,
        {
            "amount_minor": amount,
            "bank_reference": key,
            "received_at": s.clock[0].isoformat() + "Z",
            "evidence": "Выписка банка подтверждена ответственным",
        },
    )


def allocate(s, o, r, amount, key="allocation"):
    return f.allocate(s.db, s.ctx.contract, o.id, o.version, {"receipt_id": r.id, "amount_minor": amount}, key)


def debt(s):
    o = invoice(s, obligation(s))
    w = f.open_debt(s.db, s.ctx.contract, o.id, o.version, {"evidence": "Проверены срок, счёт и остаток"})
    return o, w


def da(s, w, action, data=None, actor=None):
    return f.debt_action(s.db, actor or s.ctx.contract, w.id, w.version, action, data or {})


def test_signatures_and_edo_invitation_are_separate_facts(fin):
    s = fin
    act(s, "package", {"document_reference": "doc", "reason": "Подготовлен"})
    act(s, "invitation_accepted", evidence(s))
    assert not s.a.active and not s.a.data["signatures"]
    with pytest.raises(BusinessError, match="отправки"):
        act(s, "signed_partner", evidence(s, package=s.a.data["package"]))
    act(s, "edo_sent", evidence(s))
    act(s, "signed_partner", evidence(s, package=s.a.data["package"]))
    with pytest.raises(BusinessError, match="обеих"):
        act(s, "activate", {})


def test_document_revision_invalidates_only_its_signatures(fin):
    s = fin
    active(s)
    o = obligation(s)
    r = receipt(s)
    allocate(s, o, r, 5000)
    old = s.a.data["package"]
    act(s, "package", {"document_reference": "doc2", "reason": "Дополнение условий"})
    assert s.a.data["package"] == old + 1 and not s.a.data["signatures"] and f.balance(s.db, o) == 5000
    with pytest.raises(Conflict):
        act(s, "signed_uk", evidence(s, package=old))


def test_sysadmin_and_fp_cannot_verify_money(fin):
    s = fin
    active(s)
    for actor in [s.ctx.admin, s.ctx.fp]:
        with pytest.raises(AccessDenied):
            f.receipt(
                s.db,
                actor,
                s.a.id,
                {
                    "amount_minor": 100,
                    "bank_reference": "x",
                    "received_at": BASE.isoformat() + "Z",
                    "evidence": "Скрин",
                },
            )


def test_partial_payments_leave_real_debt_and_bank_operations_are_deduped(fin):
    s = fin
    o = obligation(s)
    r = receipt(s, 6000)
    assert receipt(s, 6000).id == r.id
    allocate(s, o, r, 4000)
    assert f.balance(s.db, o) == 6000
    allocate(s, o, r, 2000, "allocation2")
    assert f.balance(s.db, o) == 4000
    with pytest.raises(BusinessError, match="больше"):
        allocate(s, o, r, 1, "dup")
    assert s.db.scalar(select(func.count()).select_from(FinanceReceipt)) == 1


def test_one_receipt_cannot_be_spent_twice_across_invoices(fin):
    s = fin
    o1 = obligation(s, "one")
    o2 = obligation(s, "two")
    r = receipt(s, 10000)
    allocate(s, o1, r, 7000)
    with pytest.raises(BusinessError):
        allocate(s, o2, r, 7000, "allocation2")
    allocate(s, o2, r, 3000, "allocation3")
    assert f.balance(s.db, o1) == 3000 and f.balance(s.db, o2) == 7000


def test_receipt_other_partner_or_other_bot_cannot_be_allocated(fin):
    s = fin
    o = obligation(s)
    r = receipt(s)
    r.data = {**r.data, "partner_id": "partner2"}
    with pytest.raises(AccessDenied):
        allocate(s, o, r, 100)
    r.data = {**r.data, "partner_id": "partner"}
    r.bot_id = "other-bot"
    with pytest.raises(AccessDenied):
        allocate(s, o, r, 100)


def test_franchise_payment_cannot_be_rebooked_as_invoice_receipt(fin):
    s = fin
    active(s)
    lead = Lead(person_id="other", source="test", consent_at=BASE, consent_version="v1", owner_id="consultant", data={})
    s.db.add(lead)
    s.db.flush()
    deal = Deal(lead_id=lead.id, required_minor=10000, data={})
    s.db.add(deal)
    s.db.flush()
    s.db.add(Payment(deal_id=deal.id, source_key="wire-used", amount_minor=10000, confirmed_by="contract"))
    s.db.flush()
    with pytest.raises(BusinessError, match="франшизы"):
        receipt(s, 10000, "wire-used")


def test_absent_invoice_does_not_move_contractual_due(fin):
    s = fin
    o = obligation(s)
    date = o.due_at
    with pytest.raises(BusinessError, match="счёт"):
        f.open_debt(s.db, s.ctx.contract, o.id, o.version, {"evidence": "Не оплачено"})
    invoice(s, o)
    assert o.due_at == date
    w = f.open_debt(s.db, s.ctx.contract, o.id, o.version, {"evidence": "Проверено"})
    assert w.data["stage"] == "identified" and f.balance(s.db, o) == 10000


def test_seven_days_start_only_from_delivery_to_curator(fin):
    s = fin
    o, w = debt(s)
    da(s, w, "curator")
    assert w.owner_id == "contract" and w.data["stage"] == "curator_delivery" and not w.data.get("control_due")
    row = s.db.scalar(select(Outbox).where(Outbox.payload["finance_curator_delivery"].as_string() == w.id))
    row.status = "failed"
    s.clock[0] += timedelta(days=10)
    assert not f.debt_tick(s.db)
    row.status = "sent"
    f.delivered(s.db, row)
    assert f.parse_date(w.data["control_due"]) == s.clock[0] + timedelta(days=7)
    s.clock[0] += timedelta(days=7)
    assert f.debt_tick(s.db) and w.data["stage"] == "curator_check"
    assert w.owner_id == "contract"


def test_formal_stage_needs_both_email_and_edo_and_three_days(fin):
    s = fin
    o, w = debt(s)
    w.data = {**w.data, "stage": "curator_check"}
    data = {
        "bank_review": "Проверен остаток",
        "email_evidence": "Почта отправлена",
        "email_sent_at": BASE.isoformat() + "Z",
    }
    with pytest.raises(BusinessError, match="ЭДО"):
        da(s, w, "formal", data)
    data.update(edo_evidence="ЭДО отправлено", edo_sent_at=BASE.isoformat() + "Z")
    da(s, w, "formal", data)
    s.clock[0] += timedelta(days=2)
    assert not f.debt_tick(s.db)
    s.clock[0] += timedelta(days=1)
    assert f.debt_tick(s.db) and w.data["stage"] == "formal_check"


def test_legal_handoff_preserves_request_id_until_lawyer_accepts(fin):
    s = fin
    o, w = debt(s)
    ident = w.id
    w.data = {**w.data, "stage": "formal_check"}
    da(
        s,
        w,
        "pretrial",
        {
            "bank_review": "Остаток проверен",
            "evidence": "Досудебное вручено",
            "due_at": (BASE + timedelta(days=5)).isoformat() + "Z",
        },
    )
    with pytest.raises(BusinessError):
        da(s, w, "legal", {"bank_review": "Проверено", "evidence": "Пакет"})
    s.clock[0] += timedelta(days=5)
    f.debt_tick(s.db)
    da(s, w, "legal", {"bank_review": "Остаток повторно проверен", "evidence": "Полный пакет"})
    assert w.owner_id == "contract" and w.pending_owner_id == "lawyer" and w.id == ident
    row = s.db.scalar(select(Outbox).where(Outbox.dedupe_key.like("finance-legal:%")))
    f.on_topic(s.db, row, "771")
    da(s, w, "legal_accept", actor=Actor("lawyer", w.pending_binding_id))
    assert (
        w.id == ident
        and w.owner_id == "lawyer"
        and w.data["stage"] == "legal"
        and w.data["legal_packet"]["request_id"] == ident
    )
    with pytest.raises(AccessDenied):
        da(s, w, "formal", {})


def test_zero_balance_requires_explicit_close_by_procedure_owner(fin):
    s = fin
    o, w = debt(s)
    allocate(s, o, receipt(s), 10000)
    assert w.status != "done" and f.balance(s.db, o) == 0
    da(s, w, "close", {"evidence": "Оплата проверена"})
    assert w.status == "done"


def test_zero_balance_prevents_a_new_collection_notice(fin):
    s = fin
    o, w = debt(s)
    w.data = {**w.data, "stage": "curator_check"}
    allocate(s, o, receipt(s), 10000)
    with pytest.raises(BusinessError, match="погашен"):
        da(s, w, "formal", {})


def test_dispute_records_disputed_and_undisputed_parts_without_wiping_debt(fin):
    s = fin
    o, w = debt(s)
    da(s, w, "dispute", {"disputed_minor": 3000, "evidence": "Возражение ФП"})
    assert w.data["dispute"]["undisputed_minor"] == 7000 and f.balance(s.db, o) == 10000


def test_deferral_requires_actual_leadership_decision_for_this_case(fin):
    s = fin
    o, w = debt(s)
    original = o.due_at
    decision = f.decision_request(
        s.db,
        s.ctx.contract,
        w.id,
        w.version,
        {
            "question": "Просьба об отсрочке",
            "position": "Проверены документы",
            "due_at": (BASE + timedelta(days=2)).isoformat() + "Z",
        },
    )
    data = {
        "decision": "Одобрена конкретная отсрочка",
        "basis": "Решение руководства",
        "approved": True,
        "defer_until": (BASE + timedelta(days=20)).isoformat() + "Z",
    }
    with pytest.raises(AccessDenied):
        f.record_decision(s.db, s.ctx.admin, decision.id, decision.version, data)
    f.record_decision(s.db, s.director, decision.id, decision.version, data)
    f.apply_deferral(s.db, s.ctx.contract, w.id, w.version, {"decision_id": decision.id})
    assert o.due_at == original and w.data["stage"] == "deferral_wait" and f.balance(s.db, o) == 10000


def test_monthly_batch_is_one_task_and_obligation_due_independent_of_run_day(fin):
    s = fin
    active(s)
    start = datetime(2027, 1, 31, 9)
    schedule = f.create_schedule(
        s.db,
        s.ctx.contract,
        {
            "start_at": start.isoformat() + "Z",
            "until_at": "2027-05-31T09:00:00Z",
            "timezone": "Europe/Moscow",
            "approval": "Утверждён календарь",
        },
    )
    s.clock[0] = start
    assert f.schedule_tick(s.db)
    assert s.db.scalar(select(func.count()).select_from(FinanceBatch)) == 1
    o = s.db.scalar(select(FinanceObligation).where(FinanceObligation.period == "2027-01"))
    assert o.due_at.day == 10
    assert schedule.next_at.day == 28
    s.clock[0] = schedule.next_at
    f.schedule_tick(s.db)
    assert schedule.next_at.day == 31 and schedule.next_at.month == 3


def test_billing_batch_cannot_complete_with_unsent_invoice(fin):
    s = fin
    active(s)
    start = BASE + timedelta(days=1)
    f.create_schedule(
        s.db,
        s.ctx.contract,
        {
            "start_at": start.isoformat() + "Z",
            "until_at": (start + timedelta(days=40)).isoformat() + "Z",
            "timezone": "Europe/Moscow",
            "approval": "Утверждено",
        },
    )
    s.clock[0] = start
    f.schedule_tick(s.db)
    batch = s.db.scalar(select(FinanceBatch))
    with pytest.raises(BusinessError):
        f.finish_batch(s.db, s.ctx.contract, batch.id, batch.version)
    for item in batch.data["items"]:
        invoice(s, s.db.get(FinanceObligation, item["obligation_id"]))
    f.finish_batch(s.db, s.ctx.contract, batch.id, batch.version)
    assert batch.data["status"] == "done"


def test_unconsented_contract_or_unsigned_offer_never_creates_debt(fin):
    s = fin
    with pytest.raises(BusinessError, match="активировано"):
        f.obligation(
            s.db,
            s.ctx.contract,
            s.a.id,
            s.a.version,
            {"period": "x", "amount_minor": 100, "due_at": BASE.isoformat() + "Z", "basis": "Предложение"},
        )


def test_generic_work_json_cannot_create_or_close_finance_case(fin):
    s = fin
    with pytest.raises(BusinessError, match="финансовым"):
        create_work(s.db, s.ctx.curator, {"kind": "finance_debt"}, "forged")
    o, w = debt(s)
    with pytest.raises(BusinessError, match="договорные"):
        transition(s.db, s.ctx.contract, w.id, w.version, "accept", {}, "forged-close")


def test_preview_has_no_financial_or_topic_side_effects(fin):
    s = fin
    d = business.make_draft(
        s.db,
        s.ctx.curator,
        "finance:request",
        "Договор",
        {
            "partner_id": "partner",
            "brand_id": "brand",
            "establishment_id": "est",
            "party_details": "Вводные",
            "terms": "Условия",
            "basis": "Основание",
            "due_at": (BASE + timedelta(days=10)).isoformat() + "Z",
        },
    )
    before = s.db.scalar(select(func.count()).select_from(FinanceAgreement))
    business.preview(s.db, s.ctx.curator, d)
    assert s.db.scalar(select(func.count()).select_from(FinanceAgreement)) == before


def test_finance_ui_callbacks_and_scope(fin):
    s = fin
    screen = engine.session(s.db, s.ctx.contract)
    text, buttons = controller.dispatch(s.db, s.ctx.contract, screen, "fi_agreement", {"id": s.a.id})
    assert "Подписи" in text and all(len(value.encode()) <= 64 for _, value in buttons)
    with pytest.raises(AccessDenied):
        f.authorize(s.db, s.ctx.fp, s.a)


def paid(s):
    w = create_work(
        s.db,
        s.ctx.curator,
        {
            "kind": "paid_work",
            "title": "Платная услуга",
            "partner_id": "partner",
            "brand_id": "brand",
            "establishment_id": "est",
            "owner_id": "specialist",
            "reviewer_id": "curator",
            "partner_person_id": "fp",
            "binding_id": "software",
            "client_binding_id": "client",
            "calendar": "support",
            "due_at": (BASE + timedelta(days=10)).isoformat() + "Z",
        },
        "paid-extra",
    )
    from ama.operations import operation

    actor = Actor("specialist", "software")
    operation(
        s.db,
        actor,
        w.id,
        w.version,
        "offer",
        {
            "payment_policy": s.extra.id,
            "scope": "Утверждённый объём",
            "amount_minor": 5000,
            "valid_until": (BASE + timedelta(days=2)).isoformat() + "Z",
            "deadline": (BASE + timedelta(days=10)).isoformat() + "Z",
            "start_rule": "partner_payment_evidence",
        },
    )
    return w, actor


def test_extra_partial_or_late_claim_is_recorded_without_start_or_franchise_payment(fin):
    from ama.operations import operation

    s = fin
    w, actor = paid(s)
    operation(
        s.db,
        s.ctx.fp,
        w.id,
        w.version,
        "offer_accept",
        {"offer_version": 1, "amount_minor": 2000, "evidence": "Клиентский скрин"},
    )
    assert (
        w.status == "waiting_uk"
        and not w.data.get("paid_started_at")
        and w.data["last_payment_claim"]["amount_minor"] == 2000
    )
    assert s.db.scalar(select(func.count()).select_from(Payment)) == 0


def test_extra_payment_policy_version_is_frozen_and_fp_accepts_delivered_result(fin):
    from ama.operations import operation

    s = fin
    w, actor = paid(s)
    snapshot = w.data["offer"]["recipient"].copy()
    s.extra.version += 1
    s.extra.data = {**s.extra.data, "reference": "https://example.org/new-recipient"}
    assert w.data["offer"]["recipient"] == snapshot
    operation(
        s.db,
        s.ctx.fp,
        w.id,
        w.version,
        "offer_accept",
        {"offer_version": 1, "amount_minor": 5000, "evidence": "Клиент подтвердил предусмотренный старт"},
    )
    assert w.data["paid_started_at"] and w.reviewer_id == "fp"
    transition(
        s.db, actor, w.id, w.version, "submit", {"result_description": "Готовый результат для ФП"}, "paid-submit"
    )
    with pytest.raises(BusinessError, match="доставки"):
        transition(s.db, s.ctx.fp, w.id, w.version, "accept", {}, "paid-accept-early")
    row = s.db.scalar(select(Outbox).where(Outbox.payload["finance_paid_result"]["work_id"].as_string() == w.id))
    row.status = "sent"
    f.delivered(s.db, row)
    transition(s.db, s.ctx.fp, w.id, w.version, "accept", {}, "paid-accept")
    assert w.outcome == "accepted"


def test_paid_cancellation_creates_financial_review_without_refund_or_erasing_history(fin):
    from ama.operations import operation

    s = fin
    w, actor = paid(s)
    operation(
        s.db,
        s.ctx.fp,
        w.id,
        w.version,
        "offer_accept",
        {"offer_version": 1, "amount_minor": 5000, "evidence": "Клиентское подтверждение"},
    )
    f.extra_claim(
        s.db,
        s.ctx.fp,
        w.id,
        w.version,
        {"kind": "cancel", "reason": "Нужно договорное решение", "evidence": "Заявление"},
    )
    assert w.data["financial_review_work"] and w.data["paid_started_at"] and not w.data.get("refund_executed")


def test_one_receipt_cap_holds_under_concurrent_allocations(fin):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    if fin.db.bind.dialect.name != "mysql":
        pytest.skip("Requires MySQL row locks")
    s = fin
    o1 = obligation(s, "one")
    o2 = obligation(s, "two")
    r = receipt(s, 10000)
    ids = [(o1.id, o1.version), (o2.id, o2.version)]
    s.db.commit()
    barrier = Barrier(2)

    def run(item):
        with s.ctx.factory.begin() as db:
            barrier.wait()
            try:
                f.allocate(
                    db,
                    s.ctx.contract,
                    item[0],
                    item[1],
                    {"receipt_id": r.id, "amount_minor": 7000},
                    "parallel:" + item[0],
                )
                return "ok"
            except BusinessError:
                return "blocked"

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(run, ids))
    assert sorted(results) == ["blocked", "ok"]
    s.db.expire_all()
    assert s.db.scalar(select(func.sum(FinanceAllocation.amount_minor))) == 7000


def test_late_full_client_claim_does_not_start_paid_service(fin):
    from ama.operations import operation

    s = fin
    w, actor = paid(s)
    s.clock[0] += timedelta(days=3)
    operation(
        s.db,
        s.ctx.fp,
        w.id,
        w.version,
        "offer_accept",
        {"offer_version": 1, "amount_minor": 5000, "evidence": "Поздний скрин"},
    )
    assert not w.data.get("paid_started_at") and w.status == "waiting_uk"


def test_duration_starts_from_valid_consent_and_two_hour_control_is_once(fin):
    from ama.operations import operation

    s = fin
    w, actor = paid(s)
    offer = {**w.data["offer"], "deadline_mode": "duration", "duration_minutes": 300, "duration_counter": "calendar"}
    offer.pop("deadline", None)
    operation(s.db, actor, w.id, w.version, "offer", offer)
    s.clock[0] += timedelta(hours=1)
    operation(
        s.db,
        s.ctx.fp,
        w.id,
        w.version,
        "offer_accept",
        {"offer_version": 2, "amount_minor": 5000, "evidence": "Подтверждение"},
    )
    assert w.due_at == s.clock[0] + timedelta(hours=5)
    assert not f.extra_tick(s.db)
    s.clock[0] += timedelta(hours=3)
    assert f.extra_tick(s.db)
    assert not f.extra_tick(s.db)


def test_paid_result_ui_requires_delivery_and_uses_explicit_fp_decision(fin):
    from ama.operations import operation
    from ama.ui.finance import dispatch

    s = fin
    w, actor = paid(s)
    operation(
        s.db,
        s.ctx.fp,
        w.id,
        w.version,
        "offer_accept",
        {"offer_version": 1, "amount_minor": 5000, "evidence": "Подтверждение"},
    )
    transition(s.db, actor, w.id, w.version, "submit", {"result_description": "Полный результат"}, "ui-result-submit")
    row = s.db.scalar(select(Outbox).where(Outbox.payload["finance_paid_result"]["work_id"].as_string() == w.id))
    row.status = "sent"
    f.delivered(s.db, row)
    session = engine.session(s.db, s.ctx.fp)
    dispatch(s.db, s.ctx.fp, session, "fi_extra", {"id": w.id})
    draft = business.make_draft(s.db, s.ctx.fp, "finance:extra_result_accept", "Принять", {}, w)
    f.authorize_draft(s.db, s.ctx.fp, draft)
    f.execute_draft(s.db, s.ctx.fp, draft, {})
    assert w.status == "done" and w.outcome == "accepted"


def test_paid_cancellation_review_can_record_actual_contract_department_decision(fin):
    from ama.models import Work
    from ama.operations import operation

    s = fin
    w, actor = paid(s)
    operation(
        s.db,
        s.ctx.fp,
        w.id,
        w.version,
        "offer_accept",
        {"offer_version": 1, "amount_minor": 5000, "evidence": "Подтверждение"},
    )
    f.extra_claim(s.db, s.ctx.fp, w.id, w.version, {"kind": "cancel", "reason": "Остановить и проверить расчёты"})
    review = s.db.get(Work, w.data["financial_review_work"])
    draft = business.make_draft(s.db, s.ctx.contract, "finance:decision_record", "Решение", {}, review)
    f.authorize_draft(s.db, s.ctx.contract, draft)
    f.record_decision(
        s.db,
        s.ctx.contract,
        review.id,
        review.version,
        {
            "decision": "Возврат требует отдельного подтверждения банка",
            "basis": "Утверждённый расчёт",
            "approved": True,
        },
    )
    assert review.status == "done" and not w.data.get("refund_executed")


def test_finance_recipient_publication_requires_approval(fin):
    from ama.ui.configuration import validate_record

    with pytest.raises(BusinessError):
        validate_record(
            fin.db,
            "policy",
            {"policy_type": "finance_recipient", "recipient_key": "bank", "reference": "https://example.org"},
            published=True,
        )


def test_one_monthly_queue_includes_agreements_of_other_department_owners(fin):
    s = fin
    active(s)
    s.db.add(Identity(id="second-contract", name="Второй договорник"))
    s.db.flush()
    s.db.add(Role(person_id="second-contract", name="contract"))
    s.db.flush()
    root = f.root(s.db, s.a)
    root.owner_id = root.executor_id = "second-contract"
    start = BASE + timedelta(days=1)
    schedule = f.create_schedule(
        s.db,
        s.ctx.contract,
        {
            "start_at": start.isoformat() + "Z",
            "until_at": "2027-05-31T09:00:00Z",
            "timezone": "Europe/Moscow",
            "approval": "Общий месячный пакет отдела",
        },
    )
    s.clock[0] = start
    assert f.schedule_tick(s.db)
    batch = s.db.scalar(select(FinanceBatch))
    assert [item["agreement_id"] for item in batch.data["items"]] == [s.a.id]
    assert batch.data["owner_id"] == "contract" and schedule.next_at > start
