from datetime import timedelta, datetime
from types import SimpleNamespace
import pytest
from sqlalchemy import select, func
from fastapi.testclient import TestClient
from ama import api, bot, sale_flow as flow, reminders, worker, sales, prospects
from ama.auth import Actor
from ama.config import settings
from ama.models import (
    Record,
    Role,
    Binding,
    Entity,
    Lead,
    SalesCase,
    ProspectSession,
    LandingTicket,
    SalesEvent,
    SalesControl,
    SalesExpert,
    Deal,
    DealInterest,
    Outbox,
    LeadTouch,
    now,
)
from ama.errors import BusinessError, AccessDenied, Conflict
from ama.ui import engine as ui, business


@pytest.fixture
def sales_ctx(ctx, monkeypatch):
    cal = ctx.db.scalar(select(Record).where(Record.kind == "calendar", Record.key == "support"))
    ctx.db.add(Record(kind="calendar", key="sales", data=cal.data, published=True, owner_id="admin"))
    ctx.db.add(
        Record(
            kind="route",
            key="sales",
            published=True,
            owner_id="admin",
            data={
                "binding_id": "sales",
                "owner_id": "consultant",
                "queue_manager_id": "consultant",
                "reviewer_id": "consultant",
                "calendar": "sales",
            },
        )
    )
    ctx.db.add(
        Record(
            kind="route",
            key="contracts",
            published=True,
            owner_id="admin",
            data={"binding_id": "contracts", "owner_id": "contract", "reviewer_id": "contract", "calendar": "sales"},
        )
    )
    ctx.db.add(
        Record(
            kind="consent",
            key="personal",
            published=True,
            owner_id="admin",
            data={"text": "Согласие на передачу и коммуникацию", "privacy_url": "https://example.org/privacy"},
        )
    )
    ctx.db.get(Entity, "brand").data = {"sales_available": True}
    ctx.db.add(
        Record(
            kind="policy",
            key="burger-landing",
            published=True,
            owner_id="admin",
            data={"policy_type": "source", "brand_id": "brand", "tags": ["utm_source", "campaign"]},
        )
    )
    ctx.db.add(
        Record(
            kind="policy",
            key="burger-about",
            published=True,
            owner_id="admin",
            data={
                "policy_type": "sales_content",
                "brand_id": "brand",
                "category": "about",
                "title": "О проекте",
                "text": "Только утверждённый текст",
                "approval": "Владелец",
                "approved_on": "2026-11-13",
                "public": True,
            },
        )
    )
    ctx.db.flush()
    monkeypatch.setattr(settings(), "telegram_bot_username", "ama_test_bot")
    monkeypatch.setattr(settings(), "max_bot_username", "ama_test_bot")
    return ctx


def private(ctx, text="", callback=None, user="other", counter=[100]):
    counter[0] += 1
    message = {"chat": {"id": "private-" + user, "type": "private"}, "from": {"id": user}, "text": text}
    raw = {"update_id": counter[0], "message": message}
    if callback:
        raw = {
            "update_id": counter[0],
            "callback_query": {"id": str(counter[0]), "from": {"id": user}, "message": message, "data": callback},
        }
    before = {r.id for r in ctx.db.scalars(select(Outbox))}
    bot.handle(ctx.db, "telegram", raw)
    ctx.db.flush()
    messages = [
        r
        for r in ctx.db.scalars(select(Outbox).order_by(Outbox.created_at))
        if r.id not in before and r.dedupe_key.startswith("reply:")
    ]
    assert messages
    return messages[-1].payload


def press(ctx, response, label, user="other"):
    callback = next(v for k, v in response["buttons"] if label in k)
    return private(ctx, callback=callback, user=user)


def accepted_case(ctx, person="other", **extra):
    lead = sales.lead_entry(ctx.db, person, "landing", "personal")
    reminders.react(ctx.db, lead, {"platform": "telegram", "chat_id": "private-" + person})
    case = flow.submit(
        ctx.db, lead, {"name": "Посетитель", "city": "Москва", "communication": "chat", **extra}, "brand"
    )
    ctx.db.flush()
    return lead, case


def test_landing_endpoint_filters_tags_and_carries_brand(sales_ctx, monkeypatch):
    c = sales_ctx
    c.db.commit()
    monkeypatch.setattr(api, "Session", c.factory)
    client = TestClient(api.app)
    response = client.get(
        "/landing/burger-landing?utm_source=vk&campaign=nov&password=never-store", follow_redirects=False
    )
    assert response.status_code == 302
    assert response.headers["location"].startswith("https://t.me/ama_test_bot?start=l_")
    c.db.expire_all()
    ticket = c.db.scalar(select(LandingTicket))
    assert ticket.data["tags"] == {"utm_source": "vk", "campaign": "nov"}
    msg = private(c, "/start l_" + ticket.id)
    assert "АМА Бургер" in msg["text"] and "utm_source" not in msg["text"]
    assert not c.db.scalar(select(Lead))
    assert not c.db.scalar(select(SalesCase))


def test_first_screen_call_requires_only_minimum_then_consent(sales_ctx):
    c = sales_ctx
    response = private(c, "/start")
    response = press(c, response, "Перезвоните")
    assert "обращаться" in response["text"]
    private(c, "Стас")
    private(c, "Москва")
    private(c, "+79991234567")
    response = private(c, "Сегодня после 15:00")
    assert "Согласие" in response["text"]
    assert not c.db.scalar(select(Lead))
    response = press(c, response, "Согласен")
    assert "передан" in response["text"]
    case = c.db.scalar(select(SalesCase))
    assert case.brand_id is None and case.data["phone"] == "+79991234567"
    assert len(list(c.db.scalars(select(SalesControl)))) == 2
    assert len([o for o in c.db.scalars(select(Outbox)) if o.payload.get("op") == "create_topic"]) == 1


def test_published_brand_content_only_and_economics_is_not_review(sales_ctx):
    c = sales_ctx
    response = private(c, "/start")
    response = press(c, response, "Посмотреть проекты")
    response = press(c, response, "АМА Бургер")
    response = press(c, response, "Познакомиться")
    response = press(c, response, "О проекте")
    assert "Только утверждённый текст" in response["text"]
    assert not c.db.scalar(select(SalesEvent).where(SalesEvent.kind == "FINANCIAL_REVIEW"))
    rec = c.db.scalar(select(Record).where(Record.key == "burger-about"))
    rec.published = False
    response = private(c, "/start")
    response = press(c, response, "Посмотреть цифры")
    assert "нет действующего" in response["text"]


def test_callbacks_bound_to_person_and_version(sales_ctx):
    c = sales_ctx
    response = private(c, "/start")
    callback = response["buttons"][0][1]
    assert len(callback.encode()) <= 64
    with pytest.raises(AccessDenied):
        private(c, callback=callback, user="fp")
    private(c, "/start")
    with pytest.raises(Conflict):
        private(c, callback=callback)


def test_opt_out_preserved_on_landing_but_new_question_allows_only_its_reply(sales_ctx):
    c = sales_ctx
    lead, case = accepted_case(c)
    case.owner_id = "consultant"
    private(c, "/start")
    session = c.db.scalar(select(ProspectSession))
    session.case_id = case.id
    private(c, "/stop")
    assert lead.no_contact
    private(c, "/start")
    assert lead.no_contact
    private(c, "Можно уточнить вопрос?")
    assert lead.no_contact and case.data["reply_to_inbound"]
    result = flow.action(
        c.db,
        c.consultant,
        case.id,
        case.version,
        "reply",
        {"text": "Да, отвечаю", "touch_reason": "consultant_reply"},
        "answer-optout",
    )
    assert result["id"] == case.id and lead.no_contact
    assert not c.db.scalar(select(LeadTouch))
    row = c.db.scalar(select(Outbox).where(Outbox.dedupe_key == "sales-reply:answer-optout"))
    assert row.payload["reply_to_inbound"]


def test_parallel_interest_requires_confirmation_and_success_is_immutable(sales_ctx):
    c = sales_ctx
    lead, case = accepted_case(c)
    case.result = "success"
    with pytest.raises(BusinessError):
        flow.submit(c.db, lead, {"name": "Ещё"}, "brand")
    with pytest.raises(BusinessError):
        flow.submit(c.db, lead, {"name": "Ещё"}, "brand", True)
    other = flow.submit(
        c.db, lead, {"name": "Расширение", "city": "Москва"}, "brand", True, "Явное подтверждение клиента"
    )
    assert other.id != case.id and other.lead_id == case.lead_id and other.parent_id == case.id
    assert case.result == "success" and c.db.scalar(select(func.count()).select_from(Lead)) == 1


def test_take_first_reply_timers_and_reassignment_do_not_reset_deadlines(sales_ctx):
    c = sales_ctx
    lead, case = accepted_case(c)
    before = [t.due_at for t in c.db.scalars(select(SalesControl).order_by(SalesControl.due_at))]
    flow.action(c.db, c.consultant, case.id, case.version, "take", {}, "take")
    first = [t.due_at for t in c.db.scalars(select(SalesControl).where(SalesControl.kind.like("first_reply%")))]
    c.db.add(Role(person_id="curator", name="consultant"))
    c.db.flush()
    flow.action(
        c.db, c.consultant, case.id, case.version, "reassign", {"person_id": "curator", "reason": "Замена"}, "reassign"
    )
    assert first == [t.due_at for t in c.db.scalars(select(SalesControl).where(SalesControl.kind.like("first_reply%")))]
    assert before == [
        t.due_at
        for t in c.db.scalars(
            select(SalesControl)
            .where(SalesControl.event_key.like("sales:" + case.id + ":accept:%"))
            .order_by(SalesControl.due_at)
        )
    ]


def test_sla_calendar_crosses_weekend_and_holiday(sales_ctx):
    c = sales_ctx
    cal = flow.sales_calendar(c.db)
    friday = datetime(2026, 11, 20, 16, 55)  # Friday 19:55 Moscow
    assert cal.add_minutes(friday, 10) == datetime(2026, 11, 23, 8, 5)
    assert flow.working_shift(cal, datetime(2026, 11, 23, 8, 5), -15) == datetime(2026, 11, 20, 16, 50)


def test_wait_and_pause_reopen_without_client_message(sales_ctx, monkeypatch):
    c = sales_ctx
    lead, case = accepted_case(c)
    case.owner_id = "consultant"
    due = now() + timedelta(days=2)
    flow.action(
        c.db,
        c.consultant,
        case.id,
        case.version,
        "not_now",
        {"next_contact": due.isoformat() + "+00:00", "next_action": "Позвонить", "reason": "Договорились"},
        "pause",
    )
    assert case.result == "paused"
    fixed = due + timedelta(hours=1)
    monkeypatch.setattr(flow, "now", lambda: fixed)
    # Isolate the planned contact; absolute 10/20 controls have independent cancellation.
    for control in c.db.scalars(select(SalesControl)):
        if control.kind != "contact_due":
            control.status = "cancelled"
    monkeypatch.setattr(flow, "sales_calendar", lambda db: SimpleNamespace(add_minutes=lambda value, minutes: value))
    before = [o for o in c.db.scalars(select(Outbox)) if o.payload.get("lead_id")]
    assert flow.tick(c.db)
    assert case.result is None and case.state == "active" and case.data["requires_contact"]
    assert before == [o for o in c.db.scalars(select(Outbox)) if o.payload.get("lead_id")]


def test_three_missed_calls_plan_internal_review_and_one_service_touch(sales_ctx):
    c = sales_ctx
    lead, case = accepted_case(c)
    case.owner_id = "consultant"
    c.db.add(Record(kind="template", key="lead.missed_call.1", published=True, data={"text": "Пытались дозвониться"}))
    c.db.flush()
    for i in range(3):
        flow.action(
            c.db,
            c.consultant,
            case.id,
            case.version,
            "call",
            {"result": "no_answer", "comment": "Нет ответа"},
            f"call{i}",
        )
        c.db.flush()
    assert case.data["missed_calls"] == 3 and case.state == "waiting_lead" and case.result is None
    assert len(list(c.db.scalars(select(LeadTouch)))) == 1
    assert datetime.fromisoformat(case.data["next_contact"]).replace(tzinfo=None) >= now() + timedelta(days=13)


def test_expert_answer_internal_only_and_consultant_retains_ownership(sales_ctx):
    c = sales_ctx
    lead, case = accepted_case(c)
    case.owner_id = "consultant"
    c.db.add(Role(person_id="marketing", name="chief_marketer"))
    c.db.add(Binding(id="chief", platform="max", chat_id="chief", purpose="chief_marketer", active=True, verified=True))
    c.db.add(
        Record(
            kind="route",
            key="chief_marketer",
            published=True,
            data={"binding_id": "chief", "owner_id": "marketing", "reviewer_id": "marketing", "calendar": "sales"},
        )
    )
    c.db.flush()
    flow.action(
        c.db,
        c.consultant,
        case.id,
        case.version,
        "expert",
        {
            "category": "location",
            "question": "Какой формат?",
            "due_at": (now() + timedelta(days=2)).isoformat() + "+00:00",
        },
        "expert",
    )
    assert case.owner_id == "consultant" and case.state == "waiting_chief_marketer"
    expert = c.db.scalar(select(SalesExpert))
    with pytest.raises(BusinessError):
        flow.action(c.db, c.consultant, case.id, case.version, "expert", {"question": "Повтор"}, "duplicate-expert")
    flow.expert_action(
        c.db, Actor("marketing", "chief"), expert.id, expert.version, "answer", {"answer": "Внутренняя рекомендация"}
    )
    assert expert.answer and case.owner_id == "consultant"
    assert not any(
        "Внутренняя рекомендация" in o.payload.get("text", "") and o.chat_id.startswith("private-")
        for o in c.db.scalars(select(Outbox))
    )


def test_topic_creation_durable_and_followup_card_uses_actual_thread(sales_ctx, monkeypatch):
    c = sales_ctx
    lead, case = accepted_case(c)
    c.db.commit()
    monkeypatch.setattr(worker, "Session", c.factory)

    class Transport:
        def create_topic(self, platform, chat_id, name):
            assert chat_id == "sales"
            return "987"

    assert worker.outbox_step(Transport())
    c.db.expire_all()
    case = c.db.get(SalesCase, case.id)
    bound = c.db.get(Binding, case.binding_id)
    assert bound.topic_key == "987" and bound.chat_id == "sales" and bound.purpose == "sales"
    assert c.db.scalar(select(Outbox).where(Outbox.dedupe_key.like("sales-topic-card:%"))).topic_key == "987"


def test_case_action_preview_leaves_queue_and_events_unchanged(sales_ctx):
    c = sales_ctx
    lead, case = accepted_case(c)
    d = business.make_draft(c.db, c.consultant, "case:take", "Взять", {}, target=case)
    before = c.db.scalar(select(func.count()).select_from(SalesEvent))
    response = ui.dispatch(c.db, c.consultant, ui.session(c.db, c.consultant), "preview", {"draft_id": d.id})
    assert case.owner_id is None and c.db.scalar(select(func.count()).select_from(SalesEvent)) == before
    ui.callback(c.db, c.consultant, next(v for label, v in response[1] if label == "Подтвердить применение"))
    assert case.owner_id == "consultant"


def ready_case(c, person="other", brand_id="brand", parallel=False):
    lead = c.db.scalar(select(Lead).where(Lead.person_id == person))
    if not lead:
        lead = sales.lead_entry(c.db, person, "landing", "personal")
        reminders.react(c.db, lead, {"platform": "telegram", "chat_id": "private-" + person})
    case = flow.submit(
        c.db, lead, {"name": "Партнёр", "city": "Москва"}, brand_id, parallel, "Подтверждено" if parallel else None
    )
    flow.action(c.db, c.consultant, case.id, case.version, "take", {}, "take-" + case.id)
    commercial = Record(
        kind="commercial",
        key="terms-" + case.id,
        owner_id="admin",
        published=True,
        data={
            "brand_id": brand_id,
            "required_minor": 10000,
            "terms": "Утверждённые условия",
            "approval": "Решение владельца",
            "approved_on": "2026-11-13",
        },
    )
    c.db.add(commercial)
    c.db.flush()
    flow.action(
        c.db,
        c.consultant,
        case.id,
        case.version,
        "qualify",
        {
            "commercial_key": commercial.key,
            "commercial_version": commercial.version,
            "economics_acknowledged": True,
            "intent_confirmed": True,
            "request_requisites": True,
        },
        "qualify-" + case.id,
    )
    return lead, case


def test_contract_requires_current_approved_terms_and_economics(sales_ctx):
    c = sales_ctx
    lead, case = ready_case(c)
    flow.readiness(c.db, case)
    commercial = c.db.scalar(select(Record).where(Record.key == case.data["commercial_key"]))
    commercial.version += 1
    with pytest.raises(BusinessError, match="редакцию"):
        flow.action(c.db, c.consultant, case.id, case.version, "contract", {}, "stale-contract")
    assert not c.db.scalar(select(Deal))


def test_two_interests_one_person_can_share_one_deal_and_win_once(sales_ctx):
    c = sales_ctx
    lead, first = ready_case(c)
    c.db.add(Entity(id="brand-two", kind="brand", name="Второй бренд", data={"sales_available": True}))
    c.db.flush()
    _, second = ready_case(c, brand_id="brand-two", parallel=True)
    flow.action(
        c.db, c.consultant, first.id, first.version, "contract", {"additional_case_ids": [second.id]}, "bundle-contract"
    )
    deal = c.db.scalar(select(Deal))
    assert deal.required_minor == 20000 and len(list(c.db.scalars(select(DealInterest)))) == 2
    assert c.db.scalar(select(func.count()).select_from(Lead)) == 1
    assert c.db.scalar(select(func.count()).select_from(Deal)) == 1
    with pytest.raises(BusinessError):
        sales.contract_action(c.db, c.contract, deal.id, deal.version, "confirm", {}, "early-success")
    sales.contract_action(
        c.db, c.contract, deal.id, deal.version, "package", {"document_reference": "Комплект v1"}, "package"
    )
    sales.contract_action(c.db, c.contract, deal.id, deal.version, "sent", {"evidence": "Отправлено в ЭДО"}, "sent")
    assert first.stage == "signing"
    sales.contract_action(
        c.db,
        c.contract,
        deal.id,
        deal.version,
        "payment",
        {"amount_minor": 20000, "bank_reference": "bank-bundle"},
        "payment",
    )
    with pytest.raises(BusinessError):
        sales.contract_action(c.db, c.contract, deal.id, deal.version, "confirm", {}, "still-no-signature")
    sales.contract_action(
        c.db, c.contract, deal.id, deal.version, "signed", {"evidence": "Все обязательные подписи"}, "signed"
    )
    sales.contract_action(c.db, c.contract, deal.id, deal.version, "confirm", {}, "win")
    won = deal.successful_at
    sales.contract_action(c.db, c.contract, deal.id, deal.version, "confirm", {}, "win-again")
    assert first.result == second.result == "success" and deal.successful_at == won
    assert len(list(c.db.scalars(select(SalesEvent).where(SalesEvent.kind == "DEAL_SUCCESS")))) == 1


def test_curator_clarification_preserves_win_and_acceptance_is_one_event(sales_ctx):
    c = sales_ctx
    lead, case = ready_case(c)
    flow.action(c.db, c.consultant, case.id, case.version, "contract", {}, "contract-handoff")
    deal = c.db.scalar(select(Deal))
    for action, data in [
        ("package", {"document_reference": "v1"}),
        ("signed", {"evidence": "Подписано"}),
        ("payment", {"amount_minor": 10000, "bank_reference": "handoff-bank"}),
        ("confirm", {}),
    ]:
        sales.contract_action(c.db, c.contract, deal.id, deal.version, action, data, "handoff-" + action)
    sales.contract_action(
        c.db,
        c.contract,
        deal.id,
        deal.version,
        "assign_curator",
        {"curator_id": "curator", "partner_id": "partner"},
        "assign-curator",
    )
    sales.contract_action(
        c.db, c.curator, deal.id, deal.version, "curator_clarify", {"question": "Уточните регион"}, "clarify"
    )
    assert case.result == "success" and deal.data["handoff_state"] == "clarification" and not deal.handoff_accepted_at
    for i in range(2):
        sales.contract_action(c.db, c.curator, deal.id, deal.version, "curator_accept", {}, "accept-curator" + str(i))
    assert len(list(c.db.scalars(select(SalesEvent).where(SalesEvent.kind == "CURATOR_ACCEPTED")))) == 1


def test_exception_requires_real_chief_marketer_approval(sales_ctx):
    c = sales_ctx
    lead, case = ready_case(c)
    case.data = {
        **case.data,
        "exceptions": "Индивидуальная договорённость",
        "exception_reference": "произвольный текст",
    }
    with pytest.raises(BusinessError, match="Главного маркетолога"):
        flow.readiness(c.db, case)


def test_reports_count_people_interests_and_deals_separately(sales_ctx):
    from ama.sales_reports import build

    c = sales_ctx
    lead, case = accepted_case(c)
    c.db.add(Entity(id="brand-two", kind="brand", name="Второй бренд", data={"sales_available": True}))
    c.db.flush()
    flow.submit(c.db, lead, {"name": "Тот же человек", "city": "Москва"}, "brand-two", True, "Подтверждённый интерес")
    start, end = now() - timedelta(days=1), now() + timedelta(days=1)
    columns, rows = build(c.db, c.consultant, {"report": "sales_cohort"}, start, end)
    assert dict(rows)["Лидов когорты"] == 1
    assert dict(rows)["Конверсия когорты"] == 0
    columns, rows = build(c.db, c.consultant, {"report": "sales_funnel"}, start, end)
    assert len(rows) == 2 and rows[0][0] == rows[1][0]


def test_no_auto_message_until_requested_material_actually_delivered(sales_ctx):
    c = sales_ctx
    lead, case = accepted_case(c)
    row = c.db.scalar(select(Record).where(Record.key == "burger-about"))
    row.data = {**row.data, "links": [{"title": "Презентация", "url": "https://example.org/presentation"}]}
    private(c, "/start")
    session = c.db.scalar(select(ProspectSession))
    session.case_id = case.id
    session.data = {**session.data, "brand_id": "brand"}
    response = private(c, "/start")
    response = press(c, response, "Познакомиться")
    response = press(c, response, "О проекте")
    assert not c.db.scalar(select(LeadTouch))
    row = [r for r in c.db.scalars(select(Outbox)) if r.payload.get("public_record")][-1]
    assert row.payload["public_record_version"] == 1
    c.db.add(Record(kind="template", key="lead.materials.1", published=True, data={"text": "Удалось посмотреть?"}))
    c.db.flush()
    reminders.on_delivered(c.db, row)
    assert c.db.scalar(select(LeadTouch))
    assert c.db.scalar(select(SalesEvent).where(SalesEvent.kind == "CONTENT_DELIVERED"))


def test_zero_cohort_is_not_fake_zero_conversion(sales_ctx):
    from ama.sales_reports import build

    c = sales_ctx
    columns, rows = build(
        c.db, c.consultant, {"report": "sales_cohort"}, now() - timedelta(days=1), now() + timedelta(days=1)
    )
    assert dict(rows)["Конверсия когорты"] == "Нет данных для расчёта"


def test_unknown_source_does_not_randomly_assign_a_brand(sales_ctx):
    c = sales_ctx
    url = prospects.landing_ticket(c.db, "unknown-key", "telegram", {"utm_source": "not-allowed"})
    token = url.rsplit("=", 1)[1]
    response = private(c, "/start " + token)
    assert "Выберите проект" in response["text"]
    session = c.db.scalar(select(ProspectSession))
    assert not session.data.get("brand_id")


def test_manual_reason_change_retains_global_silence_limit(sales_ctx):
    c = sales_ctx
    lead, case = accepted_case(c)
    for reason in ("materials", "consultant_reply", "missed_call"):
        for ordinal in (1, 2):
            c.db.add(
                Record(
                    kind="template", key=f"lead.{reason}.{ordinal}", published=True, data={"text": "Разрешённый текст"}
                )
            )
    c.db.flush()
    first = reminders.schedule(c.db, lead, "materials")
    c.db.flush()
    epoch = lead.data["silence"]["epoch"]
    changed = reminders.schedule(c.db, lead, "consultant_reply")
    assert first.id == changed.id and changed.reason == "consultant_reply"
    assert lead.data["silence"]["epoch"] == epoch
    lead.data = {**lead.data, "silence": {"epoch": epoch, "sent": 2}}
    assert reminders.schedule(c.db, lead, "missed_call") is None


def test_max_start_payload_normalizes_to_landing_flow(sales_ctx):
    from ama.adapters import normalize

    event = normalize(
        "max", {"update_type": "bot_started", "chat_id": 123, "user": {"user_id": 456}, "payload": "l_abc"}
    )
    assert event.private and event.text == "/start l_abc"


def test_client_reply_invalidates_unconfirmed_consultant_draft(sales_ctx):
    c = sales_ctx
    lead, case = accepted_case(c)
    case.owner_id = "consultant"
    d = business.make_draft(
        c.db,
        c.consultant,
        "case:reply",
        "Ответ",
        {"text": "Добрый день", "touch_reason": "consultant_reply"},
        target=case,
    )
    response = ui.dispatch(c.db, c.consultant, ui.session(c.db, c.consultant), "preview", {"draft_id": d.id})
    private(c, "/start")
    private(c, "Новый вопрос до отправки")
    confirm = next(value for label, value in response[1] if label == "Подтвердить применение")
    result = ui.callback(c.db, c.consultant, confirm)
    assert "Устаревшие данные" in result[0]
    assert not any(row.payload.get("sales_reply") for row in c.db.scalars(select(Outbox)))


def test_queued_sales_reply_rechecks_consultant_rights(sales_ctx, monkeypatch):
    c = sales_ctx
    lead, case = accepted_case(c)
    case.owner_id = "consultant"
    for row in c.db.scalars(select(Outbox)):
        row.status = "sent"
    flow.action(
        c.db,
        c.consultant,
        case.id,
        case.version,
        "reply",
        {"text": "Ответ", "touch_reason": "consultant_reply"},
        "revoked-reply",
    )
    c.db.scalar(select(Role).where(Role.person_id == "consultant")).active = False
    c.db.commit()
    monkeypatch.setattr(worker, "Session", c.factory)

    class Transport:
        def send(self, *args):
            pytest.fail("Отозванные права должны блокировать доставку")

    assert worker.outbox_step(Transport())
    c.db.expire_all()
    assert c.db.scalar(select(Outbox).where(Outbox.dedupe_key == "sales-reply:revoked-reply")).status == "blocked"


def test_material_unpublished_after_queue_is_not_delivered(sales_ctx, monkeypatch):
    c = sales_ctx
    material = c.db.scalar(select(Record).where(Record.key == "burger-about"))
    c.db.add(
        Outbox(
            dedupe_key="old-public-content",
            platform="telegram",
            chat_id="private-other",
            payload={
                "text": "Устаревший текст",
                "public_record": material.id,
                "public_record_version": material.version,
            },
        )
    )
    material.published = False
    c.db.commit()
    monkeypatch.setattr(worker, "Session", c.factory)

    class Transport:
        def send(self, *args):
            pytest.fail("Архивированный материал нельзя отправлять")

    assert worker.outbox_step(Transport())
    c.db.expire_all()
    assert c.db.scalar(select(Outbox).where(Outbox.dedupe_key == "old-public-content")).status == "cancelled"


def test_max_public_start_and_callback_use_same_sales_flow(sales_ctx):
    c = sales_ctx
    token = prospects.landing_ticket(c.db, "burger-landing", "max", {}).rsplit("=", 1)[1]
    bot.handle(
        c.db, "max", {"update_type": "bot_started", "chat_id": 4321, "user": {"user_id": 9876}, "payload": token}
    )
    c.db.flush()
    message = [r for r in c.db.scalars(select(Outbox)) if r.platform == "max" and r.payload.get("buttons")][-1]
    callback = next(value for label, value in message.payload["buttons"] if label == "Перезвоните мне")
    bot.handle(
        c.db,
        "max",
        {
            "update_type": "message_callback",
            "callback": {"callback_id": "max-call-click", "user": {"user_id": 9876}, "payload": callback},
            "message": {"recipient": {"chat_id": 4321, "chat_type": "dialog"}},
        },
    )
    c.db.flush()
    assert any("обращаться" in r.payload.get("text", "") for r in c.db.scalars(select(Outbox)))
    assert c.db.scalar(select(ProspectSession)).platform == "max"


def test_primary_interest_does_not_overwrite_person_wide_contact_or_silence(sales_ctx):
    c = sales_ctx
    lead, case = accepted_case(c)
    case.data = {
        **case.data,
        "contact": {"platform": "telegram", "chat_id": "old-chat"},
        "silence": {"epoch": "old", "sent": 0},
        "next_contact": None,
    }
    lead.data = {
        **lead.data,
        "contact": {"platform": "max", "chat_id": "new-confirmed-chat"},
        "silence": {"epoch": "new", "sent": 1},
        "next_contact": "2026-12-01T12:00:00+03:00",
    }
    flow.sync(c.db, case)
    assert lead.data["contact"]["chat_id"] == "new-confirmed-chat"
    assert lead.data["silence"] == {"epoch": "new", "sent": 1}
    assert lead.data["next_contact"] == "2026-12-01T12:00:00+03:00"


def test_browsing_another_brand_cannot_overwrite_confirmed_interest(sales_ctx):
    c = sales_ctx
    lead, case = accepted_case(c)
    c.db.add(Entity(id="other-brand", kind="brand", name="Другой проект", data={"sales_available": True}))
    c.db.flush()
    response = private(c, "/start")
    response = press(c, response, "Другие проекты")
    response = press(c, response, "Другой проект")
    response = press(c, response, "Только посмотреть")
    response = press(c, response, "Консультант в чате")
    assert "отдельный интерес" in response["text"]
    response = press(c, response, "Продолжить текущий")
    response = press(c, response, "Согласен")
    assert case.brand_id == "brand" and len(list(c.db.scalars(select(SalesCase)))) == 1


def test_existing_customer_resumes_expansion_without_touching_previous_success(sales_ctx):
    c = sales_ctx
    lead, original = accepted_case(c)
    original.result = "success"
    c.db.add(Entity(id="expansion-brand", kind="brand", name="Новая покупка", data={"sales_available": True}))
    c.db.flush()
    expansion = flow.submit(
        c.db, lead, {"name": "Партнёр", "city": "Казань"}, "expansion-brand", True, "Клиент подтвердил новую покупку"
    )
    response = private(c, "/start")
    response = press(c, response, "Консультант в чате")
    response = press(c, response, "Согласен")
    session = c.db.scalar(select(ProspectSession))
    assert session.case_id == expansion.id and original.result == "success"
    assert len(list(c.db.scalars(select(SalesCase)))) == 2


def test_bundle_preview_shows_sum_and_rejects_changed_secondary_interest(sales_ctx):
    c = sales_ctx
    lead, first = ready_case(c)
    c.db.add(Entity(id="bundle-extra", kind="brand", name="Дополнительный", data={"sales_available": True}))
    c.db.flush()
    _, second = ready_case(c, brand_id="bundle-extra", parallel=True)
    draft = business.make_draft(
        c.db, c.consultant, "case:contract", "Передача пакета", {"additional_case_ids": [second.id]}, target=first
    )
    response = ui.dispatch(c.db, c.consultant, ui.session(c.db, c.consultant), "preview", {"draft_id": draft.id})
    assert "200.00 ₽" in response[0]
    assert not c.db.scalar(select(Deal))
    second.version += 1
    with pytest.raises(Conflict):
        business.confirm(c.db, c.consultant, draft, draft.version)
    assert not c.db.scalar(select(Deal))
