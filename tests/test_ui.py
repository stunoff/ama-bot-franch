"""Employee flows through the same durable screens used by Telegram and MAX."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import pytest
from sqlalchemy import select, func
from ama import bot, worker, admin
from ama.auth import Actor
from ama.adapters import Incoming, normalize
from ama.config import settings
from ama.errors import AccessDenied, BusinessError, Conflict
from ama.models import (UIScreen, UIDraft, ConfigurationRevision, Record, Role, Account, Binding, Entity,
                        Work, Outbox, Timer, UIExport, now)
from ama.ui import engine as ui, business, controller, reports, access, configuration


def click(ctx, actor, response, label):
    choices = [payload for title, payload in response[1] if label in title]
    assert len(choices) == 1, (label, [b[0] for b in response[1]])
    result = ui.callback(ctx.db, actor, choices[0])
    ctx.db.flush()
    return result


def open_screen(ctx, actor, screen_action="home", **params):
    response = ui.dispatch(ctx.db, actor, ui.session(ctx.db, actor), screen_action, params)
    ctx.db.flush()
    assert all(len(callback.encode()) <= 64 for _, callback in response[1])
    return response


def fill(ctx, actor, draft, path, value):
    response = open_screen(ctx, actor, "field", draft_id=draft.id, path=path)
    assert response[1]
    response = ui.input_text(ctx.db, actor, value)
    ctx.db.flush()
    return response


def pending(ctx):
    return ctx.db.scalar(select(func.count()).select_from(Outbox))


def test_admin_start_and_settings_have_human_buttons(ctx):
    bot.handle(ctx.db, "telegram", {"update_id": 1, "message": {"from": {"id": "admin"}, "chat": {"id": "management", "type": "supergroup"}, "text": "/af_settings"}})
    ctx.db.flush()
    message = ctx.db.scalar(select(Outbox))
    assert "Настройки АМА" in message.payload["text"]
    assert "Сотрудники и роли" in [b[0] for b in message.payload["buttons"]]
    assert message.payload["ui_actor"] == "admin"
    assert not message.payload["text"].startswith("{")
    with pytest.raises(AccessDenied):
        open_screen(ctx, ctx.curator, "settings")


def test_client_menu_is_preserved(ctx):
    bot.handle(ctx.db, "telegram", {"update_id": 2, "message": {"from": {"id": "fp"}, "chat": {"id": "client", "type": "supergroup"}, "text": "/start"}})
    ctx.db.flush()
    row = ctx.db.scalar(select(Outbox))
    assert "Главное меню" in row.payload["text"]
    assert not row.payload.get("ui_actor")
    assert not ctx.db.scalar(select(UIScreen))


@pytest.mark.parametrize("platform", ["telegram", "max"])
def test_native_callback_opens_same_durable_ui(ctx, platform):
    if platform == "max":
        ctx.db.add(Account(person_id="admin", platform="max", external_id="100"))
        ctx.db.add(Binding(id="max-admin", platform="max", chat_id="200", purpose="management", active=True, verified=True))
        ctx.db.flush()
        actor = Actor("admin", "max-admin")
    else:
        actor = ctx.admin
    response = open_screen(ctx, actor, "settings")
    payload = next(p for t, p in response[1] if t == "Сотрудники и роли")
    raw = {"update_id": 7, "callback_query": {"id": "callback", "from": {"id": "admin"}, "data": payload,
           "message": {"chat": {"id": "management", "type": "supergroup"}}}} if platform == "telegram" else {
           "update_type": "message_callback", "callback": {"callback_id": "callback", "user": {"user_id": 100}, "payload": payload},
           "message": {"recipient": {"chat_id": 200}}}
    assert normalize(platform, raw).callback == payload
    bot.handle(ctx.db, platform, raw)
    ctx.db.flush()
    messages = list(ctx.db.scalars(select(Outbox)))
    assert any("Сотрудники и роли" in r.payload.get("text", "") for r in messages)
    assert any(r.payload.get("callback_id") for r in messages)


def test_callbacks_actor_chat_and_version_bound(ctx):
    response = open_screen(ctx, ctx.admin, "settings")
    callback = response[1][0][1]
    with pytest.raises(AccessDenied):
        ui.callback(ctx.db, ctx.curator, callback)
    with pytest.raises(AccessDenied):
        ui.callback(ctx.db, Actor("admin", "support"), callback)
    open_screen(ctx, ctx.admin, "settings")
    with pytest.raises(Conflict):
        ui.callback(ctx.db, ctx.admin, callback)


def test_employee_creation_preview_is_atomic_and_confirmation_not_reusable(ctx):
    d = controller.new_draft(ctx.db, ctx.admin, "employee", {"name": "Новый", "platform": "telegram", "external_id": "12345", "role": "curator", "reason": "Приказ"})
    response = open_screen(ctx, ctx.admin, "preview", draft_id=d.id)
    assert not ctx.db.scalar(select(Account).where(Account.external_id == "12345"))
    assert ctx.db.scalar(select(func.count()).select_from(ConfigurationRevision)) == 0
    result = click(ctx, ctx.admin, response, "Подтвердить применение")
    assert "Подтверждено" in result[0]
    assert ctx.db.scalar(select(Account).where(Account.external_id == "12345"))
    assert ctx.db.scalar(select(func.count()).select_from(ConfigurationRevision)) == 3
    with pytest.raises(Conflict):
        ui.callback(ctx.db, ctx.admin, response[1][0][1])


def material_draft(ctx):
    return controller.new_draft(ctx.db, ctx.admin, "record:material", {"key": "welcome", "owner_id": "admin", "value": {
        "title": "Инструкция", "url": "https://example.org/manual", "audience": ["partner"]}, "reason": "Утверждено"})


def test_new_material_preview_save_restart_and_publish(ctx):
    d = material_draft(ctx)
    response = open_screen(ctx, ctx.admin, "preview", draft_id=d.id, publish=True)
    assert not ctx.db.scalar(select(Record).where(Record.key == "welcome"))
    click(ctx, ctx.admin, response, "Вернуться к редактированию")
    open_screen(ctx, ctx.admin, "draft_save", draft_id=d.id)
    ctx.db.commit()
    other = ctx.factory()
    try:
        screen = ui.session(other, ctx.admin)
        response = ui.dispatch(other, ctx.admin, screen, "draft", {"draft_id": d.id})
        assert "Инструкция" not in response[0] or "Черновик" in response[0]
        response = ui.dispatch(other, ctx.admin, screen, "preview", {"draft_id": d.id, "publish": True})
        callback = next(p for t, p in response[1] if t == "Подтвердить применение")
        ui.callback(other, ctx.admin, callback)
        other.commit()
    finally:
        other.close()
    ctx.db.expire_all()
    record = ctx.db.scalar(select(Record).where(Record.key == "welcome"))
    assert record.published and record.data["url"] == "https://example.org/manual"


def test_invalid_input_retains_prompt_and_draft(ctx):
    d = controller.new_draft(ctx.db, ctx.curator, "export")
    response = fill(ctx, ctx.curator, d, ["start"], "вчера")
    assert "Некорректное" in response[0]
    before = d.data["start"]
    assert "-" in before
    response = ui.input_text(ctx.db, ctx.curator, "01.11.2026")
    assert d.data["start"] == "2026-11-01"
    assert "Черновик" in response[0]


def test_edit_conflict_preserves_second_draft(ctx):
    rec = ctx.db.scalar(select(Record).where(Record.kind == "calendar"))
    rec.owner_id = "admin"
    first = business.from_record(ctx.db, ctx.admin, rec)
    second = business.from_record(ctx.db, ctx.admin, rec)
    ui.apply_field(ctx.db, ctx.admin, first, ["reason"], "Первый")
    ui.apply_field(ctx.db, ctx.admin, second, ["reason"], "Второй")
    response = open_screen(ctx, ctx.admin, "preview", draft_id=first.id, publish=True)
    click(ctx, ctx.admin, response, "Подтвердить применение")
    old_data = deepcopy(second.data)
    with pytest.raises(Conflict):
        open_screen(ctx, ctx.admin, "preview", draft_id=second.id, publish=True)
    assert second.data == old_data and second.status == "draft"
    assert rec.version == 2


def test_nested_course_card_add_reorder_delete_and_full_preview(ctx):
    d = controller.new_draft(ctx.db, ctx.admin, "record:course", {"key": "new_course", "owner_id": "admin", "reason": "Редакция"})
    open_screen(ctx, ctx.admin, "list_add", draft_id=d.id, path=["value", "cards"])
    for key, value in {"module": 1, "lesson": "1.1", "title": "Полное название", "text": "Полный текст " * 50}.items():
        ui.apply_field(ctx.db, ctx.admin, d, ["value", "cards", 0, key], value)
    response = open_screen(ctx, ctx.admin, "content_preview", draft_id=d.id)
    # Walk all preview pages to ensure long text remains readable, not a serialized JSON object.
    contents = response[0]
    while any(t == "Следующая страница →" for t, _ in response[1]):
        response = click(ctx, ctx.admin, response, "Следующая страница")
        contents += response[0]
    assert "Полный текст " * 50 in contents
    open_screen(ctx, ctx.admin, "list_add", draft_id=d.id, path=["value", "cards"])
    open_screen(ctx, ctx.admin, "list_move", draft_id=d.id, path=["value", "cards", 1], delta=-1)
    open_screen(ctx, ctx.admin, "list_delete", draft_id=d.id, path=["value", "cards", 0])
    assert len(d.data["value"]["cards"]) == 1 and d.data["value"]["cards"][0]["title"] == "Полное название"
    response = open_screen(ctx, ctx.admin, "preview", draft_id=d.id, publish=True)
    click(ctx, ctx.admin, response, "Подтвердить применение")
    assert ctx.db.scalar(select(Record).where(Record.key == "new_course")).published


def test_work_preview_does_not_change_owner_timer_or_queue(ctx):
    ctx.w.owner_id = ctx.w.executor_id = "curator"
    ctx.db.flush()
    baseline = pending(ctx)
    response = open_screen(ctx, ctx.curator, "work_action", work_id=ctx.w.id, expected=1, action="take")
    assert "Предпросмотр" in response[0]
    assert ctx.w.status == "new" and ctx.w.version == 1 and pending(ctx) == baseline
    click(ctx, ctx.curator, response, "Подтвердить применение")
    assert ctx.w.status == "in_progress" and ctx.w.version == 2
    assert pending(ctx) == baseline+1
    assert ctx.db.scalar(select(Timer).where(Timer.work_id == ctx.w.id, Timer.status == "pending"))


def test_submit_reject_wait_and_handoff_forms(ctx):
    ctx.w.owner_id = ctx.w.executor_id = "specialist"
    ctx.w.status = "in_progress"
    response = open_screen(ctx, ctx.specialist, "work", work_id=ctx.w.id)
    assert "Результат готов…" in [t for t, _ in response[1]]
    assert "Принять результат…" not in [t for t, _ in response[1]]
    response = click(ctx, ctx.specialist, response, "Результат готов")
    d = ctx.db.scalar(select(UIDraft).where(UIDraft.kind == "work:submit"))
    fill(ctx, ctx.specialist, d, ["result_description"], "Настроено")
    response = open_screen(ctx, ctx.specialist, "preview", draft_id=d.id)
    click(ctx, ctx.specialist, response, "Подтвердить применение")
    assert ctx.w.status == "review" and ctx.w.owner_id == "curator"
    response = open_screen(ctx, ctx.curator, "work", work_id=ctx.w.id)
    assert "Вернуть на доработку…" in [t for t, _ in response[1]]
    response = click(ctx, ctx.curator, response, "Вернуть на доработку")
    d = ctx.db.scalar(select(UIDraft).where(UIDraft.kind == "work:reject"))
    fill(ctx, ctx.curator, d, ["reason"], "Нужна правка")
    fill(ctx, ctx.curator, d, ["due_at"], (now()+timedelta(days=4)).isoformat()+"+00:00")
    response = open_screen(ctx, ctx.curator, "preview", draft_id=d.id)
    click(ctx, ctx.curator, response, "Подтвердить применение")
    assert ctx.w.status == "rework" and ctx.w.owner_id == "specialist" and ctx.w.returns == 1


def test_staff_old_callback_now_requires_preview(ctx):
    ctx.w.owner_id = ctx.w.executor_id = "curator"
    raw = {"update_id": 20, "callback_query": {"id": "old", "from": {"id": "curator"}, "data": "af:a:work:1:take",
           "message": {"chat": {"id": "support", "type": "supergroup"}}}}
    bot.handle(ctx.db, "telegram", raw)
    ctx.db.flush()
    assert ctx.w.status == "new"
    assert ctx.db.scalar(select(UIDraft)).status == "checked"


def test_last_available_admin_cannot_be_revoked_even_if_other_expired(ctx):
    ctx.db.add(Role(person_id="other", name="admin", expires_at=now()-timedelta(days=1)))
    ctx.db.flush()
    role = ctx.db.scalar(select(Role).where(Role.person_id == "admin"))
    with pytest.raises(BusinessError, match="последнего"):
        admin.apply(ctx.db, ctx.admin, {"type": "revoke_role", "data": {"id": role.id, "version": 1}})


def test_scoped_dashboard_and_report_exclude_foreign_partner(ctx):
    role = ctx.db.scalar(select(Role).where(Role.person_id == "curator"))
    role.partner_id = "partner"
    ctx.db.add(Work(id="foreign", title="Секрет другого ФП", kind="support", partner_id="partner2", binding_id="support",
                    owner_id="other", executor_id="other", reviewer_id="other", due_at=now()+timedelta(days=2), original_due_at=now()+timedelta(days=2)))
    ctx.db.flush()
    assert "foreign" not in [w.id for w in access.works(ctx.db, ctx.curator)]
    result = reports.rows(ctx.db, ctx.curator, {"report": "works", "start": (now()-timedelta(days=2)).date().isoformat(), "end": now().date().isoformat()})
    assert "foreign" not in [row[0] for row in result["rows"]]
    with pytest.raises(AccessDenied):
        open_screen(ctx, ctx.curator, "work", work_id="foreign")


def test_export_preview_no_file_then_delivers_csv_with_injection_escaped(ctx, monkeypatch, tmp_path):
    monkeypatch.setattr(settings(), "file_root", str(tmp_path))
    monkeypatch.setattr(worker, "Session", ctx.factory)
    ctx.w.title = "=DANGEROUS()"
    d = controller.new_draft(ctx.db, ctx.curator, "export")
    response = open_screen(ctx, ctx.curator, "preview", draft_id=d.id)
    assert not list(tmp_path.rglob("*.csv"))
    assert not ctx.db.scalar(select(UIExport)) and pending(ctx) == 0
    click(ctx, ctx.curator, response, "Подтвердить применение")
    ctx.db.commit()
    delivered = []
    class Transport:
        def send(self, platform, chat_id, topic, payload, file_path):
            delivered.append((chat_id, payload, Path(file_path).read_text(encoding="utf-8-sig")))
            return "message"
    assert worker.outbox_step(Transport())
    assert len(delivered) == 1 and delivered[0][0] == "support"
    assert "'=DANGEROUS()" in delivered[0][2]
    assert delivered[0][1]["filename"].endswith(".csv")


def test_queued_ui_message_blocked_after_role_revocation(ctx, monkeypatch):
    monkeypatch.setattr(worker, "Session", ctx.factory)
    event = Incoming(key="u", platform="telegram", user_id="curator", chat_id="support", topic="", text="/start", callback=None, callback_id=None, private=False, files=[])
    ui.reply(ctx.db, ctx.curator, event, open_screen(ctx, ctx.curator))
    role = ctx.db.scalar(select(Role).where(Role.person_id == "curator"))
    role.active = False
    ctx.db.commit()
    class Transport:
        def send(self, *args):
            pytest.fail("Revoked user's report/menu must not be sent")
    assert worker.outbox_step(Transport())
    ctx.db.expire_all()
    assert ctx.db.scalar(select(Outbox)).status == "blocked"


def test_marketing_project_cannot_edit_opening(ctx):
    actor = Actor("marketing", "marketing")
    d = controller.new_draft(ctx.db, actor, "project", {"partner_id": "partner", "establishment_id": "est", "scenario": "new", "blocks": [{
        "key": "official_opening", "title": "Открытие", "mode": "perform", "owner_id": "marketing", "reviewer_id": "marketing", "binding_id": "marketing", "calendar": "support", "due_at": (now()+timedelta(days=1)).isoformat()+"+00:00", "data": {"expected_result": "Открыто"}}]})
    with pytest.raises(BusinessError, match="маркетинговый"):
        business.preview(ctx.db, actor, d)
    assert ctx.db.scalar(select(func.count()).select_from(Work)) == 1


def test_project_form_creates_work_without_json_project_id(ctx):
    d = controller.new_draft(ctx.db, ctx.curator, "project", {"partner_id": "partner", "establishment_id": "est", "scenario": "new", "blocks": [{
        "key": "support", "title": "Подготовить", "mode": "perform", "owner_id": "curator", "reviewer_id": "curator", "binding_id": "support", "calendar": "support", "due_at": (now()+timedelta(days=1)).isoformat()+"+00:00", "data": {"expected_result": "Готово"}}]})
    response = open_screen(ctx, ctx.curator, "preview", draft_id=d.id)
    assert ctx.db.scalar(select(func.count()).select_from(Work)) == 1
    click(ctx, ctx.curator, response, "Подтвердить применение")
    assert ctx.db.scalar(select(Work).where(Work.title == "Подготовить")).project_id == d.id


def test_broadcast_audience_change_requires_new_preview(ctx):
    ctx.db.get(Entity, "partner").data = {"notification_window": {"timezone": "Europe/Moscow", "weekdays": [0,1,2,3,4], "start": "11:00", "end": "20:00"}}
    d = controller.new_draft(ctx.db, ctx.curator, "broadcast", {"text": "Информация"})
    response = open_screen(ctx, ctx.curator, "preview", draft_id=d.id)
    assert pending(ctx) == 0
    ctx.db.get(Binding, "client").version += 1
    response = click(ctx, ctx.curator, response, "Подтвердить применение")
    assert "Аудитория" in response[0]
    assert d.status == "checked" and pending(ctx) == 0


def test_diagnostics_safe_retry_requires_confirm(ctx):
    row = Outbox(dedupe_key="failed", platform="telegram", chat_id="support", payload={"text": "x"}, status="failed", attempts=2)
    ctx.db.add(row)
    ctx.db.flush()
    response = open_screen(ctx, ctx.admin, "diagnostic_retry", queue="delivery", object_id=row.id)
    d = ctx.db.scalar(select(UIDraft).where(UIDraft.kind == "config:retry_delivery"))
    fill(ctx, ctx.admin, d, ["reason"], "Исправлена сеть")
    response = open_screen(ctx, ctx.admin, "preview", draft_id=d.id)
    assert row.status == "failed"
    click(ctx, ctx.admin, response, "Подтвердить применение")
    assert row.status == "pending" and row.attempts == 0


def test_scenario_cycle_and_archive_dependencies(ctx):
    with pytest.raises(BusinessError, match="цикл"):
        configuration.validate_record(ctx.db, "policy", {"policy_type": "scenario", "blocks": [{"key": "a", "depends_on": ["b"]}, {"key": "b", "depends_on": ["a"]}]})
    rec = ctx.db.scalar(select(Record).where(Record.kind == "calendar"))
    assert configuration.dependencies(ctx.db, rec)
    with pytest.raises(BusinessError, match="зависимостями"):
        admin.apply(ctx.db, ctx.admin, {"type": "archive_record", "data": {"id": rec.id, "version": rec.version, "reason": "Архив"}})


def test_attachment_context_pins_work_version(ctx):
    open_screen(ctx, ctx.curator, "upload", work_id="work", expected=1)
    assert ui.attachment_context(ctx.db, ctx.curator) == "work"
    ctx.w.version += 1
    with pytest.raises(Conflict):
        ui.attachment_context(ctx.db, ctx.curator)


@pytest.mark.parametrize("role,binding", [("curator", "support"), ("marketing", "marketing"), ("specialist", "design"), ("consultant", "sales"), ("contract", "contracts")])
def test_each_employee_dashboard_and_queues_open(ctx, role, binding):
    actor = Actor(role, binding)
    response = open_screen(ctx, actor)
    assert "Рабочее место" in response[0]
    for action in ("works", "drafts", "materials"):
        response = open_screen(ctx, actor, action)
        assert response[0]
    if role == "curator":
        for action in ("partners", "partner", "enterprises", "enterprise", "projects"):
            params = {"partner_id": "partner"} if action == "partner" else {"establishment_id": "est"} if action == "enterprise" else {}
            assert open_screen(ctx, actor, action, **params)[0]


def test_lead_qualification_preserves_confirmed_messenger_route(ctx):
    from ama.models import Lead
    lead = Lead(person_id="fp", source="bot", consent_version="v1", consent_at=now(), owner_id="consultant",
                data={"contact": {"platform": "telegram", "chat_id": "123"}})
    ctx.db.add(lead)
    ctx.db.flush()
    response = open_screen(ctx, ctx.consultant, "lead_action", lead_id=lead.id, expected=lead.version, action="qualify")
    d = ctx.db.scalar(select(UIDraft).where(UIDraft.kind == "sales:qualify"))
    for key, value in {"name": "Лид", "contact": "+79990000000", "city": "Москва", "brand_id": "brand"}.items():
        ui.apply_field(ctx.db, ctx.consultant, d, [key], value)
    response = open_screen(ctx, ctx.consultant, "preview", draft_id=d.id)
    click(ctx, ctx.consultant, response, "Подтвердить применение")
    assert lead.data["contact"] == {"platform": "telegram", "chat_id": "123"}
    assert lead.data["contact_description"] == "+79990000000"
    response = open_screen(ctx, ctx.consultant, "lead_action", lead_id=lead.id, expected=lead.version, action="reply")
    d = ctx.db.scalar(select(UIDraft).where(UIDraft.kind == "sales:reply"))
    fill(ctx, ctx.consultant, d, ["text"], "Здравствуйте")
    response = open_screen(ctx, ctx.consultant, "preview", draft_id=d.id)
    assert not ctx.db.scalar(select(Outbox))
    click(ctx, ctx.consultant, response, "Подтвердить применение")
    assert ctx.db.scalar(select(Outbox)).chat_id == "123"


def test_temporary_substitution_keeps_due_and_returns_owner(ctx):
    ctx.w.owner_id = ctx.w.executor_id = "curator"
    due, original = ctx.w.due_at, ctx.w.original_due_at
    timer = Timer(work_id="work", kind="deadline", due_at=due, original_due_at=due, key="existing-timer", data={"version": 1})
    ctx.db.add(timer)
    d = controller.new_draft(ctx.db, ctx.admin, "substitution", {"from_person": "curator", "to_person": "other", "work_ids": ["work"],
        "expires_at": (now()+timedelta(days=1)).isoformat()+"+00:00", "reason": "Отпуск"})
    response = open_screen(ctx, ctx.admin, "preview", draft_id=d.id)
    assert ctx.w.owner_id == "curator" and timer.data["version"] == 1
    click(ctx, ctx.admin, response, "Подтвердить применение")
    assert ctx.w.owner_id == ctx.w.reviewer_id == "other"
    assert ctx.w.due_at == due and ctx.w.original_due_at == original and timer.data["version"] == 2
    d.data = {**d.data, "expires_at": (now()-timedelta(days=1)).isoformat()+"+00:00"}
    assert configuration.substitution_step(ctx.db)
    assert ctx.w.owner_id == ctx.w.reviewer_id == "curator" and d.status == "returned"
    assert ctx.w.due_at == due and timer.data["version"] == 3


def test_unknown_delivery_reconciliation_requires_evidence_and_preview(ctx):
    row = Outbox(dedupe_key="uncertain", platform="telegram", chat_id="support", payload={"text": "x"}, status="uncertain", attempts=1)
    ctx.db.add(row)
    ctx.db.flush()
    response = open_screen(ctx, ctx.admin, "diagnostic_item", queue="delivery", object_id=row.id)
    response = click(ctx, ctx.admin, response, "Зафиксировать проверенный исход")
    d = ctx.db.scalar(select(UIDraft).where(UIDraft.kind == "config:reconcile_delivery"))
    for key, value in {"outcome": "delivered", "external_id": "message42", "evidence": "Проверено в чате", "reason": "Сверка"}.items():
        ui.apply_field(ctx.db, ctx.admin, d, [key], value)
    response = open_screen(ctx, ctx.admin, "preview", draft_id=d.id)
    assert row.status == "uncertain"
    click(ctx, ctx.admin, response, "Подтвердить применение")
    assert row.status == "sent" and row.external_id == "message42"


def test_binding_probe_activate_and_safe_replace_through_forms(ctx, monkeypatch):
    monkeypatch.setattr(worker, "Session", ctx.factory)
    b = Binding(platform="telegram", chat_id="replacement", purpose="support", active=False, verified=False)
    ctx.db.add(b)
    ctx.db.flush()
    response = open_screen(ctx, ctx.admin, "config_action", catalog="bindings", object_id=b.id, command="probe_binding")
    d = ctx.db.scalar(select(UIDraft).where(UIDraft.kind == "config:probe_binding"))
    fill(ctx, ctx.admin, d, ["reason"], "Проверка нового чата")
    response = open_screen(ctx, ctx.admin, "preview", draft_id=d.id)
    assert pending(ctx) == 0
    click(ctx, ctx.admin, response, "Подтвердить применение")
    assert not b.verified and not b.active
    ctx.db.commit()
    class Transport:
        def send(self, *args):
            return "probe-message"
    assert worker.outbox_step(Transport())
    ctx.db.expire_all()
    assert b.verified and not b.active
    response = open_screen(ctx, ctx.admin, "config_action", catalog="bindings", object_id="support", command="replace_binding")
    d = ctx.db.scalar(select(UIDraft).where(UIDraft.kind == "config:replace_binding"))
    ui.apply_field(ctx.db, ctx.admin, d, ["new_id"], b.id)
    fill(ctx, ctx.admin, d, ["reason"], "Переезд очереди")
    due = ctx.w.due_at
    response = open_screen(ctx, ctx.admin, "preview", draft_id=d.id)
    assert ctx.w.binding_id == "support"
    click(ctx, ctx.admin, response, "Подтвердить применение")
    assert ctx.w.binding_id == b.id and ctx.w.id == "work" and ctx.w.due_at == due
    assert b.active and not ctx.db.get(Binding, "support").active


def test_role_and_binding_revoked_block_saved_buttons(ctx):
    response = open_screen(ctx, ctx.curator)
    callback = response[1][0][1]
    role = ctx.db.scalar(select(Role).where(Role.person_id == "curator"))
    role.active = False
    with pytest.raises(AccessDenied):
        ui.callback(ctx.db, ctx.curator, callback)


def test_wait_handoff_reschedule_are_confirmed_with_original_deadline(ctx):
    ctx.w.owner_id = ctx.w.executor_id = "curator"
    due = (now()+timedelta(days=5)).isoformat()+"+00:00"
    original = ctx.w.original_due_at
    response = open_screen(ctx, ctx.curator, "work_action", work_id="work", expected=1, action="wait_external")
    d = ctx.db.scalar(select(UIDraft).where(UIDraft.kind == "work:wait_external"))
    for key, value in {"party": "Подрядчик", "required": "Материал", "next_check": due}.items():
        ui.apply_field(ctx.db, ctx.curator, d, [key], value)
    response = open_screen(ctx, ctx.curator, "preview", draft_id=d.id)
    click(ctx, ctx.curator, response, "Подтвердить применение")
    assert ctx.w.status == "waiting_external"
    response = open_screen(ctx, ctx.curator, "work_action", work_id="work", expected=ctx.w.version, action="handoff")
    d = ctx.db.scalar(select(UIDraft).where(UIDraft.kind == "work:handoff"))
    ui.apply_field(ctx.db, ctx.curator, d, ["person_id"], "specialist")
    ui.apply_field(ctx.db, ctx.curator, d, ["binding_id"], "design")
    response = open_screen(ctx, ctx.curator, "preview", draft_id=d.id)
    click(ctx, ctx.curator, response, "Подтвердить применение")
    assert ctx.w.owner_id == "curator" and ctx.w.pending_owner_id == "specialist"
    actor = Actor("specialist", "design")
    response = open_screen(ctx, actor, "work_action", work_id="work", expected=ctx.w.version, action="handoff_accept")
    click(ctx, actor, response, "Подтвердить применение")
    assert ctx.w.owner_id == "specialist" and ctx.w.binding_id == "design"
    assert ctx.w.original_due_at == original


def test_existing_record_history_restore_uses_new_version(ctx):
    rec = ctx.db.scalar(select(Record).where(Record.kind == "calendar"))
    rec.owner_id = "admin"
    for start in ("10:00", "12:00"):
        d = business.from_record(ctx.db, ctx.admin, rec)
        ui.apply_field(ctx.db, ctx.admin, d, ["value", "start"], start)
        ui.apply_field(ctx.db, ctx.admin, d, ["reason"], "Расписание")
        response = open_screen(ctx, ctx.admin, "preview", draft_id=d.id, publish=True)
        click(ctx, ctx.admin, response, "Подтвердить применение")
    revision = ctx.db.scalar(select(ConfigurationRevision).where(ConfigurationRevision.object_id == rec.id, ConfigurationRevision.new_version == 2))
    response = open_screen(ctx, ctx.admin, "rollback", revision_id=revision.id)
    d = ctx.db.scalars(select(UIDraft).where(UIDraft.status == "draft")).one()
    response = open_screen(ctx, ctx.admin, "preview", draft_id=d.id, publish=True)
    assert rec.data["start"] == "12:00"
    click(ctx, ctx.admin, response, "Подтвердить применение")
    assert rec.data["start"] == "10:00" and rec.version == 4


def test_dual_role_does_not_enable_curator_actions_in_marketing_chat(ctx):
    actor = Actor("marketing", "marketing")
    ctx.db.add(Role(person_id="marketing", name="curator"))
    ctx.db.flush()
    data = {"partner_id": "partner", "establishment_id": "est", "scenario": "new", "blocks": [{
        "key": "official_opening", "title": "Открытие", "mode": "perform", "owner_id": "marketing", "reviewer_id": "marketing",
        "binding_id": "marketing", "calendar": "support", "due_at": (now()+timedelta(days=2)).isoformat()+"+00:00", "data": {"expected_result": "Открыто"}}]}
    d = controller.new_draft(ctx.db, actor, "project", data)
    with pytest.raises(BusinessError, match="маркетинговый"):
        business.preview(ctx.db, actor, d)
    d = controller.new_draft(ctx.db, actor, "create_work", {"partner_id": "partner", "kind": "official_opening"})
    with pytest.raises(AccessDenied, match="маркетинговые"):
        business.authorize(ctx.db, actor, d)


def test_substitution_reviewer_must_match_specialized_acceptance_role(ctx):
    ctx.w.kind = "marketing.analytics"
    ctx.w.reviewer_id = "marketing"
    d = controller.new_draft(ctx.db, ctx.admin, "substitution", {"from_person": "marketing", "to_person": "curator", "work_ids": ["work"], "reason": "Замещение"})
    with pytest.raises(BusinessError, match="подходящей"):
        business.preview(ctx.db, ctx.admin, d)
    assert ctx.w.reviewer_id == "marketing" and ctx.w.version == 1
