from datetime import datetime, timedelta
import pytest
from sqlalchemy import select, func
from ama import sla
from ama.auth import Actor
from ama.calendar import Calendar
from ama.errors import BusinessError, AccessDenied
from ama.models import SLAEvent, SLASignal, Outbox, Binding, Role, Record, ProjectPlan
from ama.service import transition, create_work
from ama.sla_rules import defaults
from ama.ui import business, access, engine as ui

BASE = datetime(2027, 1, 4, 9)


@pytest.fixture
def timebox(monkeypatch):
    box = [BASE]
    monkeypatch.setattr(sla, "now", lambda: box[0])
    monkeypatch.setattr("ama.service.now", lambda: box[0])
    return box


def setup(ctx, timebox, purpose="support", priority="normal", category="ordinary", explicit=False):
    w = ctx.w
    b = ctx.db.get(Binding, "new")
    b.purpose = purpose
    w.binding_id = "new"
    w.review_binding_id = "support"
    w.owner_id = w.executor_id = "specialist"
    w.reviewer_id = "curator"
    w.created_at = BASE
    w.priority = priority
    w.due_at = w.original_due_at = BASE + timedelta(days=10)
    w.data = {
        **w.data,
        "sla_category": category,
        "incident": "Касса не принимает заказы; подтверждён аварийный отказ" if priority == "critical" else None,
    }
    ctx.db.flush()
    c = sla.initialize(ctx.db, w, explicit_due=explicit)
    ctx.db.flush()
    return w, c, Actor("specialist", "new")


def pending(ctx, w, kind):
    return ctx.db.scalar(
        select(SLAEvent).where(SLAEvent.work_id == w.id, SLAEvent.kind == kind, SLAEvent.status == "pending")
    )


def act(ctx, actor, w, name, data=None):
    data = dict(data or {})
    if name == "wait_partner":
        data.setdefault("reason", "Без указанного поля нельзя выполнить это действие")
    if name == "wait_external":
        data.setdefault("sent_information", "Анкета и исходные документы переданы контрагенту")
    return transition(ctx.db, actor, w.id, w.version, name, data or {}, "test:" + name + ":" + str(w.version))


def drain(ctx, limit=60):
    for _ in range(limit):
        if not sla.tick(ctx.db):
            return
        ctx.db.flush()
    raise AssertionError("SLA scheduler did not become idle")


@pytest.mark.parametrize(
    "purpose,priority,take,result",
    [
        ("support", "normal", ["days", 1], ["days", 2]),
        ("support", "important", ["minutes", 240], ["today", 0]),
        ("support", "urgent", ["minutes", 60], ["minutes", 60]),
        ("marketing", "normal", ["days", 1], ["days", 3]),
        ("technical_support", "normal", ["minutes", 240], ["days", 1]),
        ("technical_support", "urgent", ["minutes", 60], ["minutes", 240]),
        ("technical_support", "critical", ["minutes", 30], ["minutes", 60]),
        ("product", "normal", ["days", 1], ["days", 3]),
        ("personnel", "normal", ["days", 1], ["days", 3]),
        ("registrations", "normal", ["days", 1], ["days", 3]),
    ],
)
def test_direction_priority_matrix(purpose, priority, take, result):
    r = defaults(purpose, priority)
    assert r["take"] == take and r["result"] == result


def test_working_clock_handles_holiday_weekend_and_day_before():
    cal = Calendar(
        {
            "timezone": "Europe/Moscow",
            "start": "11:00",
            "end": "20:00",
            "weekdays": [0, 1, 2, 3, 4],
            "holidays": ["2027-01-11"],
            "extra_workdays": [],
        }
    )
    friday = datetime(2027, 1, 8, 16, 30)
    assert cal.add_minutes(friday, 60) == datetime(2027, 1, 12, 8, 30)
    assert cal.shift_workdays(friday, 2) == datetime(2027, 1, 13, 16, 30)
    assert cal.shift_workdays(datetime(2027, 1, 12, 12), -1) == datetime(2027, 1, 8, 12)
    assert cal.end_today(datetime(2027, 1, 9, 12)) == datetime(2027, 1, 12, 17)


def test_take_and_result_are_distinct_and_do_not_reset_by_attachment_version(ctx, timebox):
    w, c, actor = setup(ctx, timebox)
    take = pending(ctx, w, "take").due_at
    result = pending(ctx, w, "result").due_at
    assert take < result
    w.version += 10
    assert pending(ctx, w, "take").due_at == take
    act(ctx, actor, w, "take")
    assert not pending(ctx, w, "take") and pending(ctx, w, "result").due_at == result


def test_wait_partner_starts_reminders_only_after_confirmed_delivery(ctx, timebox):
    w, c, actor = setup(ctx, timebox)
    act(ctx, actor, w, "take")
    original = w.original_due_at
    act(
        ctx,
        actor,
        w,
        "wait_partner",
        {
            "party": "ФП: отсутствует адрес",
            "required": "Адрес и фото фасада",
            "next_check": (BASE + timedelta(days=9)).isoformat() + "Z",
        },
    )
    assert not pending(ctx, w, "partner2") and not pending(ctx, w, "result")
    row = ctx.db.scalar(select(Outbox).where(Outbox.dedupe_key.like("sla-request:%")))
    row.status = "uncertain"
    sla.delivered(ctx.db, row)
    assert not pending(ctx, w, "partner2")
    timebox[0] = BASE + timedelta(days=1)
    row.status = "sent"
    sla.delivered(ctx.db, row)
    anchor = c.data["request"]["delivered_at"]
    assert pending(ctx, w, "partner2").due_at == sla.clock(c).shift_workdays(timebox[0], 2)
    sla.delivered(ctx.db, row)
    assert c.data["request"]["delivered_at"] == anchor
    assert w.original_due_at == original


def test_fp_day5_escalates_to_curator_and_management_without_closing(ctx, timebox):
    w, c, actor = setup(ctx, timebox)
    act(ctx, actor, w, "take")
    act(
        ctx,
        actor,
        w,
        "wait_partner",
        {"party": "ФП", "required": "Реквизиты", "next_check": (BASE + timedelta(days=20)).isoformat() + "Z"},
    )
    row = ctx.db.scalar(select(Outbox).where(Outbox.dedupe_key.like("sla-request:%")))
    row.status = "sent"
    sla.delivered(ctx.db, row)
    timebox[0] = pending(ctx, w, "partner5").due_at + timedelta(seconds=1)
    drain(ctx)
    assert w.status == "waiting_partner"
    signal = ctx.db.scalar(select(SLASignal).where(SLASignal.kind.like("ФП не ответил%")))
    assert signal and not signal.data["curator_only"]
    assert ctx.db.scalar(select(Outbox).where(Outbox.dedupe_key.like("sla-fp5:%")))


def test_resume_cancels_queued_reminders_and_old_undelivered_request(ctx, timebox):
    w, c, actor = setup(ctx, timebox)
    act(ctx, actor, w, "take")
    act(
        ctx,
        actor,
        w,
        "wait_partner",
        {"party": "ФП", "required": "Фото", "next_check": (BASE + timedelta(days=9)).isoformat() + "Z"},
    )
    row = ctx.db.scalar(select(Outbox).where(Outbox.dedupe_key.like("sla-request:%")))
    assert sla.deliverable(ctx.db, row)
    act(ctx, actor, w, "resume", {"due_at": (BASE + timedelta(days=10)).isoformat() + "Z"})
    assert not sla.deliverable(ctx.db, row)


def test_wait_external_keeps_result_and_controls_next_check(ctx, timebox):
    w, c, actor = setup(ctx, timebox)
    act(ctx, actor, w, "take")
    original = pending(ctx, w, "result").due_at
    act(
        ctx,
        actor,
        w,
        "wait_external",
        {
            "party": "Банк",
            "required": "Сведения отправлены: анкета №2",
            "next_check": (BASE + timedelta(days=1)).isoformat() + "Z",
        },
    )
    assert pending(ctx, w, "result").due_at == original
    assert pending(ctx, w, "check")
    timebox[0] = original + timedelta(seconds=1)
    drain(ctx)
    assert w.data["overdue"] and w.status == "waiting_external"


def test_overdue_is_preserved_when_paused_before_worker_processed_it(ctx, timebox):
    w, c, actor = setup(ctx, timebox)
    act(ctx, actor, w, "take")
    original = pending(ctx, w, "result").due_at
    timebox[0] = original + timedelta(minutes=1)
    act(
        ctx,
        actor,
        w,
        "wait_partner",
        {"party": "ФП", "required": "Размеры", "next_check": (timebox[0] + timedelta(days=3)).isoformat() + "Z"},
    )
    assert w.data["overdue"] and c.data["breaches"]
    assert w.original_due_at == original


def test_review_overdue_belongs_to_reviewer_and_is_not_reset_by_work_edit(ctx, timebox):
    w, c, actor = setup(ctx, timebox)
    act(ctx, actor, w, "take")
    act(ctx, actor, w, "submit", {"result_description": "Результат готов"})
    e = pending(ctx, w, "review")
    assert e.data["responsible_id"] == "curator"
    w.version += 1
    timebox[0] = e.due_at + timedelta(seconds=1)
    drain(ctx)
    s = ctx.db.scalar(select(SLASignal).where(SLASignal.kind.like("Приёмка%")))
    assert s.data["responsible_id"] == "curator"


def test_plan_reminders_and_repeat_rejection_keep_same_work_and_history(ctx, timebox):
    w, c, actor = setup(ctx, timebox)
    act(ctx, actor, w, "take")
    original = w.original_due_at
    deadline = BASE + timedelta(days=10)
    act(ctx, actor, w, "sla_plan", {"due_at": deadline.isoformat() + "Z", "reason": "Согласован макет и этапы"})
    assert pending(ctx, w, "before").due_at == sla.clock(c).shift_workdays(deadline, -1)
    assert pending(ctx, w, "day").due_at.date() == deadline.date()
    for i in range(2):
        act(ctx, actor, w, "submit", {"result_description": "Редакция " + str(i)})
        act(ctx, ctx.curator, w, "reject", {"reason": "Исправить пункт", "due_at": deadline.isoformat() + "Z"})
    assert w.returns == 2 and w.id == "work" and w.original_due_at == original
    assert ctx.db.scalar(select(SLASignal).where(SLASignal.kind.like("Повторный возврат%")))


def test_marketing_requires_plan_within_two_days_not_arbitrary_10day_auto_due(ctx, timebox):
    w, c, actor = setup(ctx, timebox, "marketing", category="analysis")
    assert not pending(ctx, w, "result") and pending(ctx, w, "plan")
    act(ctx, actor, w, "take")
    with pytest.raises(BusinessError):
        act(
            ctx,
            actor,
            w,
            "sla_plan",
            {"due_at": (BASE + timedelta(days=40)).isoformat() + "Z", "reason": "Сложная задача"},
        )
    act(
        ctx,
        actor,
        w,
        "sla_plan",
        {"due_at": (BASE + timedelta(days=10)).isoformat() + "Z", "reason": "План исследования"},
    )
    assert not pending(ctx, w, "plan") and pending(ctx, w, "result")


def test_software_input_clock_starts_after_take(ctx, timebox):
    w, c, actor = setup(ctx, timebox, "software", category="new_connection")
    assert not pending(ctx, w, "input")
    timebox[0] += timedelta(hours=2)
    act(ctx, actor, w, "take")
    assert pending(ctx, w, "input").due_at == sla.clock(c).end_of_workday(timebox[0], 1)
    with pytest.raises(BusinessError):
        act(ctx, actor, w, "sla_complete_data", {"evidence": "Анкета полная"})
    act(
        ctx,
        actor,
        w,
        "sla_complete_data",
        {
            "evidence": "Анкета проверена",
            "due_at": (BASE + timedelta(days=4)).isoformat() + "Z",
            "reason": "Все подключения к дате",
        },
    )
    assert not pending(ctx, w, "input") and pending(ctx, w, "result")


def test_critical_cadence_continues_after_missed_status_and_stops_only_explicitly(ctx, timebox):
    w, c, actor = setup(ctx, timebox, "technical_support", "critical")
    assert pending(ctx, w, "take").due_at == BASE + timedelta(minutes=30)
    assert pending(ctx, w, "result").due_at == BASE + timedelta(minutes=60)
    act(ctx, actor, w, "take")
    act(ctx, actor, w, "sla_status", {"status_text": "Диагностика кассы", "evidence": "Логи проверены"})
    pulse = pending(ctx, w, "critical")
    timebox[0] = pulse.due_at + timedelta(seconds=1)
    drain(ctx)
    assert pending(ctx, w, "critical") and c.data["critical_active"]
    with pytest.raises(BusinessError):
        act(ctx, actor, w, "submit", {"result_description": "Закрыть без восстановления"})
    with pytest.raises(BusinessError):
        act(ctx, actor, w, "reschedule", {"reason": "Перенос", "due_at": (BASE + timedelta(days=10)).isoformat() + "Z"})
    act(
        ctx,
        actor,
        w,
        "sla_recovered",
        {"status_text": "Заказы принимаются", "evidence": "Проверен тестовый заказ", "resolution": "restored"},
    )
    assert not pending(ctx, w, "critical") and not c.data["critical_active"]
    assert w.status != "done"


def test_critical_requires_real_incident_and_technical_route(ctx, timebox):
    with pytest.raises(BusinessError):
        defaults("support", "critical")
    w = ctx.w
    w.priority = "critical"
    w.data = {"calendar": "support"}
    ctx.db.get(Binding, "support").purpose = "technical_support"
    with pytest.raises(BusinessError, match="Критичность"):
        sla.initialize(ctx.db, w)


def test_admin_cannot_react_to_management_signal_but_support_head_can(ctx, timebox):
    w, c, actor = setup(ctx, timebox)
    s = sla.signal(ctx.db, c, w, "test", "Просрочено взятие")
    with pytest.raises(AccessDenied):
        sla.signal_action(
            ctx.db,
            ctx.admin,
            s.id,
            s.version,
            {"reaction": "Проверено", "next_check": (BASE + timedelta(days=2)).isoformat() + "Z"},
        )
    ctx.db.add(Role(person_id="curator", name="support_head"))
    ctx.db.flush()
    head = Actor("curator", "management")
    oldowner = w.owner_id
    sla.signal_action(
        ctx.db,
        head,
        s.id,
        s.version,
        {"reaction": "Согласован план устранения", "next_check": (BASE + timedelta(days=2)).isoformat() + "Z"},
    )
    assert s.owner_id == "curator" and w.owner_id == oldowner
    timebox[0] = BASE + timedelta(days=2, seconds=1)
    drain(ctx)
    assert ctx.db.scalar(select(Outbox).where(Outbox.dedupe_key.like("sla-reaction:%")))


def test_signal_and_event_dedup_and_route_recovery(ctx, timebox):
    w, c, actor = setup(ctx, timebox)
    ctx.db.get(Binding, "management").active = False
    s = sla.signal(ctx.db, c, w, "same", "Контроль результата")
    assert not s.data["notified"]
    assert sla.signal(ctx.db, c, w, "same", "Контроль результата").id == s.id
    ctx.db.get(Binding, "management").active = True
    assert sla.tick(ctx.db)
    assert s.data["notified"]
    assert ctx.db.scalar(select(func.count()).select_from(SLASignal)) == 1


def test_profile_snapshot_survives_admin_edits(ctx, timebox):
    ctx.db.add(
        Record(
            kind="route",
            key="newroute",
            published=True,
            data={"binding_id": "new", "calendar": "support", "owner_id": "specialist", "reviewer_id": "curator"},
        )
    )
    policy = Record(
        kind="policy",
        key="sla1",
        published=True,
        data={
            "policy_type": "sla",
            "route": "newroute",
            "priority": "normal",
            "take_minutes": 90,
            "result_minutes": 180,
            "counter": "working",
            "approval": "Утверждено руководителем",
        },
    )
    ctx.db.add(policy)
    ctx.db.flush()
    w, c, actor = setup(ctx, timebox)
    original = pending(ctx, w, "take").due_at
    policy.data = {**policy.data, "take_minutes": 5}
    policy.version += 1
    assert pending(ctx, w, "take").due_at == original and c.data["policy"]["version"] == 1


def test_legacy_work_is_not_reinterpreted_until_explicit_adoption(ctx, timebox):
    assert not sla.case_for(ctx.db, ctx.w)
    ctx.w.owner_id = "curator"
    ctx.w.executor_id = "curator"
    original = ctx.w.original_due_at
    act(ctx, ctx.curator, ctx.w, "sla_adopt")
    assert sla.case_for(ctx.db, ctx.w).data["legacy"]
    assert ctx.w.original_due_at == original


def test_partner_own_request_requires_final_confirmation_and_can_return(ctx, timebox):
    # create_work marks the provenance itself; the partner cannot forge service fields.
    w = create_work(ctx.db, ctx.fp, {"kind": "support", "title": "Проблема кассы"}, "own-request")
    w.created_at = BASE
    act(ctx, ctx.curator, w, "take")
    act(ctx, ctx.curator, w, "submit", {"result_description": "Исправлено"})
    act(ctx, ctx.curator, w, "accept")
    assert w.status == "review" and w.reviewer_id == "fp" and w.outcome is None
    c = sla.case_for(ctx.db, w)
    assert c.data["request"]["confirmation"]
    row = ctx.db.scalar(select(Outbox).where(Outbox.dedupe_key == "sla-request:" + c.data["request"]["token"]))
    row.status = "sent"
    sla.delivered(ctx.db, row)
    assert pending(ctx, w, "partner5") and not pending(ctx, w, "review")
    sla.partner_return(ctx.db, ctx.fp, w, w.version, {"reason": "Ошибка осталась"}, "fp-return")
    assert w.status == "rework" and w.reviewer_id == "curator"


def test_signal_preview_is_rolled_back_and_real_confirmation_rechecks_grants(ctx, timebox):
    w, c, actor = setup(ctx, timebox)
    ctx.db.add(Role(person_id="curator", name="support_head"))
    ctx.db.flush()
    head = Actor("curator", "management")
    s = sla.signal(ctx.db, c, w, "preview", "Просрочена проверка")
    d = business.make_draft(
        ctx.db,
        head,
        "signal:react",
        "Реакция",
        {"reaction": "Проверить исполнение", "next_check": (BASE + timedelta(days=2)).isoformat() + "Z"},
        target=s,
    )
    business.preview(ctx.db, head, d)
    assert s.status == "open" and s.owner_id is None
    d.status = "checked"
    d.before = {"_checked": {"grants": access.grants_hash(ctx.db, head)}}
    business.confirm(ctx.db, head, d, d.version)
    assert s.status == "acknowledged"


def test_software_opening_cutoff_and_late_input_risk(ctx, timebox):
    w, c, actor = setup(ctx, timebox, "software", category="new_connection")
    ctx.db.add(
        ProjectPlan(
            id="p",
            bot_id="ama-franchising",
            title="План",
            partner_id="partner",
            establishment_id="est",
            brand_id="brand",
            curator_id="curator",
            scenario="new",
            profile={},
            dates={"planned_technical": (BASE + timedelta(days=10)).isoformat() + "Z"},
        )
    )
    ctx.db.flush()
    w.project_id = "p"
    cutoff = sla.opening_check(ctx.db, c, w)
    assert cutoff == BASE + timedelta(days=5)
    act(ctx, actor, w, "take")
    with pytest.raises(BusinessError, match="5 календарных"):
        act(
            ctx,
            actor,
            w,
            "sla_complete_data",
            {
                "evidence": "Полные данные",
                "reason": "Подключение",
                "due_at": (BASE + timedelta(days=8)).isoformat() + "Z",
            },
        )
    timebox[0] = BASE + timedelta(days=6)
    sla.opening_check(ctx.db, c, w)
    assert ctx.db.scalar(select(SLASignal).where(SLASignal.critical.is_(True)))


def test_late_take_and_late_critical_status_are_recorded_without_worker(ctx, timebox):
    w, c, actor = setup(ctx, timebox, "technical_support", "critical")
    timebox[0] = BASE + timedelta(minutes=40)
    act(ctx, actor, w, "take")
    assert any(b["kind"] == "take" for b in c.data["breaches"])
    timebox[0] = BASE + timedelta(minutes=80)
    act(ctx, actor, w, "sla_status", {"status_text": "Диагностика продолжается", "evidence": "Собраны логи"})
    assert any(b["kind"] == "result" for b in c.data["breaches"])
    timebox[0] = BASE + timedelta(minutes=210)
    act(ctx, actor, w, "sla_status", {"status_text": "Найден отказ базы", "evidence": "Лог базы"})
    assert any(b["kind"] == "critical" for b in c.data["breaches"])


def test_failed_delivery_creates_configuration_control_not_fp_reminders(ctx, timebox):
    w, c, actor = setup(ctx, timebox)
    act(ctx, actor, w, "take")
    act(
        ctx,
        actor,
        w,
        "wait_partner",
        {"party": "ФП", "required": "Договор", "next_check": (BASE + timedelta(days=4)).isoformat() + "Z"},
    )
    row = ctx.db.scalar(select(Outbox).where(Outbox.dedupe_key.like("sla-request:%")))
    row.status = "failed"
    sla.delivery_problem(ctx.db, row)
    assert not pending(ctx, w, "partner2")
    signal = ctx.db.scalar(select(SLASignal).where(SLASignal.kind.like("Запрос ФП не доставлен%")))
    assert signal.data["curator_only"]


def test_queued_partner_reminder_cancelled_when_wait_is_resumed(ctx, timebox):
    w, c, actor = setup(ctx, timebox)
    act(ctx, actor, w, "take")
    act(
        ctx,
        actor,
        w,
        "wait_partner",
        {"party": "ФП", "required": "Согласование", "next_check": (BASE + timedelta(days=20)).isoformat() + "Z"},
    )
    row = ctx.db.scalar(select(Outbox).where(Outbox.dedupe_key.like("sla-request:%")))
    row.status = "sent"
    sla.delivered(ctx.db, row)
    timebox[0] = pending(ctx, w, "partner2").due_at + timedelta(seconds=1)
    drain(ctx)
    reminder = ctx.db.scalar(select(Outbox).where(Outbox.dedupe_key.like("sla-notice:%")))
    assert sla.deliverable(ctx.db, reminder)
    act(ctx, actor, w, "resume", {"due_at": (timebox[0] + timedelta(days=3)).isoformat() + "Z"})
    assert not sla.deliverable(ctx.db, reminder)


def test_fp_own_confirmation_day5_goes_only_to_curator(ctx, timebox):
    w, c, actor = setup(ctx, timebox)
    w.status = "review"
    w.reviewer_id = w.partner_person_id
    w.owner_id = w.partner_person_id
    sla.invalidate(ctx.db, c, w, "take", "result")
    sla.review(ctx.db, c, w)
    row = ctx.db.scalar(select(Outbox).where(Outbox.dedupe_key.like("sla-request:%")))
    row.status = "sent"
    sla.delivered(ctx.db, row)
    timebox[0] = pending(ctx, w, "partner5").due_at + timedelta(seconds=1)
    drain(ctx)
    s = ctx.db.scalar(select(SLASignal).where(SLASignal.kind.like("ФП не ответил%")))
    assert s.data["curator_only"] and w.status == "review"


def test_client_return_form_uses_own_identity_and_preview(ctx, timebox):
    w, c, actor = setup(ctx, timebox)
    w.data = {**w.data, "partner_request": True, "technical_reviewer_id": "curator"}
    w.status = "review"
    w.owner_id = w.reviewer_id = "fp"
    d = business.make_draft(
        ctx.db, ctx.fp, "client_sla:return", "Проблема не решена", {"reason": "Касса всё ещё не работает"}, target=w
    )
    assert ui.view_draft(ctx.db, ctx.fp, d.id)
    business.preview(ctx.db, ctx.fp, d)
    assert w.status == "review"
    d.status = "checked"
    d.before = {"_checked": {"grants": access.grants_hash(ctx.db, ctx.fp)}}
    business.confirm(ctx.db, ctx.fp, d, d.version)
    assert w.status == "rework" and w.reviewer_id == "curator"


def test_actual_outbox_success_anchors_fp_controls(ctx, timebox, monkeypatch):
    from ama import worker

    w, c, actor = setup(ctx, timebox)
    act(ctx, actor, w, "take")
    act(
        ctx,
        actor,
        w,
        "wait_partner",
        {"party": "ФП", "required": "Фото", "next_check": (BASE + timedelta(days=10)).isoformat() + "Z"},
    )
    ctx.db.commit()
    monkeypatch.setattr(worker, "Session", ctx.factory)

    class FakeTransport:
        def send(self, *args):
            return "telegram-message-123"

    for _ in range(10):
        worker.outbox_step(FakeTransport())
        ctx.db.rollback()
        ctx.db.expire_all()
        if sla.case_for(ctx.db, w).data["request"].get("delivered_at"):
            break
    assert sla.case_for(ctx.db, w).data["request"]["delivered_at"]
    assert pending(ctx, w, "partner2")


def test_mysql_two_workers_create_one_escalation(ctx, timebox):
    if ctx.db.bind.dialect.name != "mysql":
        pytest.skip("MySQL row locks required")
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    w, c, actor = setup(ctx, timebox)
    timebox[0] = pending(ctx, w, "take").due_at + timedelta(seconds=1)
    ctx.db.commit()
    barrier = Barrier(2)

    def process(_):
        with ctx.factory.begin() as db:
            barrier.wait(timeout=10)
            return sla.tick(db)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(process, range(2)))
    ctx.db.expire_all()
    assert ctx.db.scalar(select(func.count()).select_from(SLASignal).where(SLASignal.work_id == w.id)) == 1
    assert ctx.db.scalar(select(func.count()).select_from(Outbox).where(Outbox.dedupe_key.like("sla-signal:%"))) == 1


def test_long_product_work_takes_with_explicit_plan_in_same_transaction(ctx, timebox):
    w, c, actor = setup(ctx, timebox, "product", category="complex")
    with ctx.db.begin_nested():
        with pytest.raises(BusinessError, match="вместе со взятием"):
            with ctx.db.begin_nested():
                act(ctx, actor, w, "take")
    assert w.status == "new"
    response = ui.dispatch(
        ctx.db,
        actor,
        ui.session(ctx.db, actor),
        "work_action",
        {"work_id": w.id, "expected": w.version, "action": "take"},
    )
    assert "Плановый результат" in response[0]
    act(
        ctx,
        actor,
        w,
        "take",
        {"due_at": (BASE + timedelta(days=7)).isoformat() + "Z", "reason": "Проверка новой упаковки и поставщика"},
    )
    assert c.data["plan"] and pending(ctx, w, "result") and w.status == "in_progress"


def test_worker_does_not_process_another_bots_sla(ctx, timebox):
    w, c, actor = setup(ctx, timebox)
    ctx.db.get(Binding, "new").bot_id = "another-bot"
    timebox[0] = pending(ctx, w, "take").due_at + timedelta(days=1)
    assert not sla.tick(ctx.db)
    assert not ctx.db.scalar(select(SLASignal))


def test_background_breach_survives_action_from_session_with_cached_case(ctx, timebox):
    w, c, actor = setup(ctx, timebox)
    act(ctx, actor, w, "take")
    timebox[0] = pending(ctx, w, "result").due_at + timedelta(seconds=1)
    ctx.db.commit()
    assert not c.data["breaches"]
    with ctx.factory.begin() as other:
        assert sla.tick(other)
    # expire_on_commit=False deliberately retains the stale UI case in this session.
    act(
        ctx,
        actor,
        w,
        "reschedule",
        {"reason": "Согласован новый этап", "due_at": (timebox[0] + timedelta(days=3)).isoformat() + "Z"},
    )
    assert c.data["breaches"] and c.data["breaches"][0]["kind"] == "result"
    ctx.db.commit()
    with ctx.factory() as check:
        assert sla.case_for(check, check.get(type(w), w.id)).data["breaches"]
