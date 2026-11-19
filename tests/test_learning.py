from datetime import datetime, timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from ama import learning as l, admin
from ama.models import LearningTrack, LearningEdition, Training, Record, Outbox, Entity
from ama.errors import BusinessError, AccessDenied, Conflict
from ama.ui import business, engine, controller


@pytest.fixture
def learn(ctx, monkeypatch):
    clock = [datetime(2027, 1, 4, 9)]
    monkeypatch.setattr(l, "now", lambda: clock[0])
    c = ctx.db.scalar(select(Record).where(Record.kind == "course", Record.key == "general"))
    c.data = {
        "cards": [
            {"module": 0, "title": "Один", "text": "Первый", "required_materials": []},
            {"module": 1, "title": "Два", "text": "Второй", "required_materials": []},
        ]
    }
    ctx.db.get(Entity, "partner").data = {
        "notification_window": {
            "timezone": "Europe/Moscow",
            "weekdays": [0, 1, 2, 3, 4, 5, 6],
            "start": "11:00",
            "end": "20:00",
        }
    }
    return SimpleNamespace(ctx=ctx, db=ctx.db, clock=clock, c=c, actor=ctx.fp)


def mat(s, key="doc", **kw):
    r = Record(
        kind="material",
        key=key,
        published=True,
        owner_id="admin",
        data={
            "title": key,
            "url": "https://example.org/" + key,
            "audience": ["partner"],
            "verified_on": "2027-01-01",
            **kw,
        },
    )
    s.db.add(r)
    s.db.flush()
    return r


def assignment(s):
    return l.assign(
        s.db, s.ctx.curator, {"person_id": "fp", "binding_id": "client", "course_key": "general", "mode": "new"}
    )


def sent(s, key):
    row = s.db.scalar(select(Outbox).where(Outbox.dedupe_key == key))
    row.status = "sent"
    l.delivered(s.db, row)
    return row


def publish(s, rec, data):
    return admin.apply(
        s.db,
        s.ctx.admin,
        {
            "type": "record",
            "data": {
                "kind": rec.kind,
                "key": rec.key,
                "version": rec.version,
                "owner_id": "admin",
                "published": True,
                "value": data,
            },
        },
    )


def test_explicit_module_and_final_confirmation_no_auto_completion(learn):
    s = learn
    t = l.action(s.db, s.actor, "general")
    with pytest.raises(BusinessError, match="раздела"):
        l.action(s.db, s.actor, "general", "next", t.version)
    t = l.action(s.db, s.actor, "general", "finish_module", t.version)
    assert t.position == 1 and not t.completed_at
    t = l.action(s.db, s.actor, "general", "finish_module", t.version)
    assert not t.completed_at
    t = l.action(s.db, s.actor, "general", "finish", t.version)
    assert t.completed_at and l.completed(s.db, "fp")


def test_observer_cannot_ack_and_other_person_does_not_inherit(learn):
    with pytest.raises(AccessDenied):
        l.action(learn.db, learn.ctx.observer, "general")
    assert not l.completed(learn.db, "other")


def test_editorial_course_version_keeps_personal_progress(learn):
    s = learn
    t = l.action(s.db, s.actor, "general")
    l.action(s.db, s.actor, "general", "finish_module", t.version)
    ident = t.id
    s.c.version += 1
    s.c.data = {**s.c.data, "title": "Редакционная правка"}
    current = l.action(s.db, s.actor, "general")
    assert current.id == ident and current.position == 1


def test_stale_buttons_cannot_advance(learn):
    s = learn
    t = l.action(s.db, s.actor, "general")
    old = t.version
    l.action(s.db, s.actor, "general", "open")
    with pytest.raises(Conflict):
        l.action(s.db, s.actor, "general", "finish_module", old)


def test_required_material_scope_checked_before_ack(learn):
    s = learn
    mat(s, brand_id="other-brand")
    s.c.data = {"cards": [{"title": "Один", "text": "Текст", "required_materials": ["doc"]}]}
    t = l.action(s.db, s.actor, "general")
    with pytest.raises(AccessDenied):
        l.action(s.db, s.actor, "general", "finish_module", t.version)
    assert not t.completed_at


def test_current_link_is_dynamic_and_edition_use_is_audited(learn):
    s = learn
    r = mat(s)
    u = l.show_material(s.db, s.actor, "doc")
    row = s.db.scalar(select(Outbox).where(Outbox.dedupe_key == "learning-material:" + u.id))
    assert u.data["fact"] == "queued_not_read" and l.deliverable(s.db, row)
    r.version += 1
    r.data = {**r.data, "url": "https://example.org/new"}
    assert not l.deliverable(s.db, row)
    new = l.show_material(s.db, s.actor, "doc")
    assert new.record_version == 2 and new.data["url"].endswith("/new")


def test_new_publication_needs_owner_attestation_and_link_checklist(learn):
    s = learn
    r = mat(s)
    with pytest.raises(BusinessError, match="владельца"):
        l.on_record(s.db, s.ctx.admin, r)
    r.data = {**r.data, "content_approval": "Утверждено"}
    with pytest.raises(BusinessError, match="четыре"):
        l.on_record(s.db, s.ctx.admin, r)
    r.data = {**r.data, "link_checks": ["opens", "audience", "exists", "title"]}
    l.on_record(s.db, s.ctx.admin, r)
    assert s.db.scalar(select(LearningEdition).where(LearningEdition.record_id == r.id))


def test_inheritance_is_not_fake_personal_completion(learn):
    s = learn
    tr = l.assign(
        s.db,
        s.ctx.curator,
        {
            "person_id": "fp",
            "binding_id": "client",
            "mode": "inherit",
            "evidence": "Подтверждён прежний курс",
            "known_version": "1.0",
        },
    )
    assert tr.status == "inherited" and l.completed(s.db, "fp")
    t = s.db.get(Training, tr.training_id)
    assert not t.completed_at and not t.acknowledged


def test_substantial_update_has_short_cards_and_preserves_old_proof(learn):
    s = learn
    t = l.action(s.db, s.actor, "general")
    t.completed_at = s.clock[0]
    tr = l.track_for(s.db, t)
    tr.status = "completed"
    oldid = t.id
    data = {
        **s.c.data,
        "content_approval": "Владелец утвердил",
        "substantial_update": True,
        "required_update_reason": "Новая обязательная роль",
        "update_cards": [{"module": 0, "title": "Что поменялось", "text": "Изменение", "required_materials": []}],
    }
    publish(s, s.c, data)
    update = s.db.scalar(select(LearningTrack).where(LearningTrack.kind == "update"))
    assert update and len(s.db.get(Training, update.training_id).cards) == 1
    assert s.db.get(Training, oldid).completed_at and not l.completed(s.db, "fp")
    assert l.action(s.db, s.actor, "general").id == update.training_id


def test_no_reminder_before_confirmed_delivery(learn):
    s = learn
    tr = assignment(s)
    s.clock[0] += timedelta(days=20)
    assert not l.tick(s.db)
    assert not tr.data.get("reservation")


def test_two_then_three_calendar_days_from_successful_delivery_only(learn):
    s = learn
    tr = assignment(s)
    sent(s, "learning-assignment:" + tr.id)
    s.clock[0] += timedelta(days=2)
    assert l.tick(s.db)
    row = s.db.get(Outbox, tr.data["reservation"])
    assert l.deliverable(s.db, row)
    row.status = "uncertain"
    assert tr.data["round"] == 0
    assert not l.tick(s.db)
    row.status = "sent"
    l.delivered(s.db, row)
    assert tr.data["round"] == 1
    s.clock[0] += timedelta(days=2)
    assert not l.tick(s.db)
    s.clock[0] += timedelta(days=1)
    assert l.tick(s.db)
    second = s.db.get(Outbox, tr.data["reservation"])
    second.status = "sent"
    l.delivered(s.db, second)
    assert tr.data["round"] == 2 and tr.data["curator_work"]
    s.clock[0] += timedelta(days=10)
    assert not l.tick(s.db)


def test_technical_pause_freezes_only_dependent_reminders(learn):
    s = learn
    r = mat(s)
    s.c.data = {"cards": [{"title": "Один", "text": "Текст", "required_materials": ["doc"]}]}
    tr = assignment(s)
    sent(s, "learning-assignment:" + tr.id)
    r.data = {**r.data, "availability": "blocked"}
    s.clock[0] += timedelta(days=2)
    l.tick(s.db)
    assert tr.status == "blocked" and not tr.data.get("reservation")
    s.clock[0] += timedelta(days=10)
    r.data = {**r.data, "availability": "active"}
    l.tick(s.db)
    assert tr.status == "pending"
    assert (l.parse_date(tr.data["delivered_at"]) - datetime(2027, 1, 4, 9)).days == 10


def test_menu_open_does_not_restart_inactivity_but_progress_does(learn):
    s = learn
    tr = assignment(s)
    sent(s, "learning-assignment:" + tr.id)
    s.clock[0] += timedelta(days=1)
    t = l.action(s.db, s.actor, "general")
    assert not tr.data["last_action"]
    l.action(s.db, s.actor, "general", "finish_module", t.version)
    assert l.parse_date(tr.data["last_action"]) == s.clock[0]


def test_missing_notification_window_goes_to_curator(learn):
    s = learn
    tr = assignment(s)
    sent(s, "learning-assignment:" + tr.id)
    s.db.get(Entity, "partner").data = {}
    s.clock[0] += timedelta(days=2)
    l.tick(s.db)
    assert tr.data["curator_work"] and not tr.data.get("reservation")


def test_manual_agreement_overrides_automatic_timer(learn):
    s = learn
    tr = assignment(s)
    sent(s, "learning-assignment:" + tr.id)
    d = business.make_draft(
        s.db,
        s.ctx.curator,
        "learning:deadline",
        "Срок",
        {"due_at": (s.clock[0] + timedelta(days=12)).isoformat() + "Z", "reason": "Согласовано с ФП"},
        tr,
    )
    business.execute(s.db, s.ctx.curator, d)
    s.clock[0] += timedelta(days=3)
    assert not l.tick(s.db)


def test_archived_material_requires_compatible_replacement_or_visible_wait(learn):
    s = learn
    r = mat(s)
    target = mat(s, "new-doc")
    l.archive(s.db, s.ctx.admin, r, "new-doc", "Утверждённая замена")
    assert l.resolve(s.db, s.actor, "doc").id == target.id
    l.archive(s.db, s.ctx.admin, target, None, "Восстанавливаем доступ")
    with pytest.raises(BusinessError):
        l.resolve(s.db, s.actor, "doc")
    assert target.data["technical_owner_id"] == "admin"


def test_internal_or_other_brand_material_never_exposed(learn):
    s = learn
    r = mat(s, internal_only=True)
    with pytest.raises(AccessDenied):
        l.show_material(s.db, s.actor, r.key)
    r.data = {**r.data, "internal_only": False, "brand_id": "other-brand"}
    with pytest.raises(AccessDenied):
        l.show_material(s.db, s.actor, r.key)


def test_general_library_gate_and_reviewed_materials_are_not_exams(learn):
    s = learn
    r = mat(s, access_stage="general_completed")
    assert not l.can_material(s.db, s.actor, r)
    assert l.can_material(s.db, s.actor, r, lesson=True)
    t = l.action(s.db, s.actor, "general")
    t.completed_at = s.clock[0]
    l.track_for(s.db, t).status = "completed"
    assert l.can_material(s.db, s.actor, r)


def test_archive_preview_is_rolled_back_and_shows_dependencies(learn):
    s = learn
    r = mat(s)
    s.c.data = {"cards": [{"title": "Текст", "text": "Карточка", "required_materials": ["doc"]}]}
    d = business.make_draft(s.db, s.ctx.admin, "learning:archive", "Замена", {"reason": "Проблема доступа"}, r)
    result = business.preview(s.db, s.ctx.admin, d)
    assert result["dependencies"] and r.published


def test_ui_callbacks_are_scoped_and_fit_platform_limit(learn):
    s = learn
    screen = engine.session(s.db, s.actor)
    text, buttons = controller.dispatch(s.db, s.actor, screen, "lr_course", {"course": "general"})
    assert "раздел 0" in text and all(len(value.encode()) <= 64 for _, value in buttons)


def test_material_substantial_update_is_personal_without_replacing_full_course(learn):
    s = learn
    r = mat(s)
    s.c.data = {"cards": [{"title": "Прежний курс", "text": "Утверждено", "required_materials": ["doc"]}]}
    t = l.action(s.db, s.actor, "general")
    t.completed_at = s.clock[0]
    l.track_for(s.db, t).status = "completed"
    data = {
        **r.data,
        "content_approval": "Владелец утвердил",
        "link_checks": ["opens", "audience", "exists", "title"],
        "substantial_update": True,
        "required_update_reason": "Изменён процесс",
        "update_cards": [{"title": "Изменение", "text": "Новый процесс", "module": 0}],
    }
    publish(s, r, data)
    update = s.db.scalar(select(LearningTrack).where(LearningTrack.kind == "update"))
    assert update and update.course_key == "general" and not l.completed(s.db, "fp")
    current = l.action(s.db, s.actor, "general")
    assert current.course_key.startswith("material_update:")
    current = l.action(s.db, s.actor, "general", "finish_module", current.version)
    l.action(s.db, s.actor, "general", "finish", current.version)
    assert l.completed(s.db, "fp")


def test_opening_reminder_selects_closest_stage_and_date_change_cancels_old(learn):
    from ama.models import ProjectPlan

    s = learn
    tr = assignment(s)
    sent(s, "learning-assignment:" + tr.id)
    p = ProjectPlan(
        bot_id=l.settings().bot_id,
        title="Открытие",
        partner_id="partner",
        establishment_id="est",
        brand_id="brand",
        curator_id="curator",
        partner_person_id="fp",
        scenario="new",
        dates={"planned_internal": (s.clock[0] + timedelta(days=1)).isoformat() + "Z"},
        profile={},
    )
    s.db.add(p)
    s.db.flush()
    assert l.opening_notice(s.db, tr)
    row = s.db.get(Outbox, tr.data["opening_reservation"])
    assert row.payload["learning_opening"]["stage"] == 1 and l.deliverable(s.db, row)
    p.dates = {**p.dates, "planned_internal": (s.clock[0] + timedelta(days=30)).isoformat() + "Z"}
    assert not l.deliverable(s.db, row)


def test_content_plan_creates_only_drafts_and_no_fake_links(learn):
    s = learn
    result = l.prepare_content(s.db, s.ctx.admin)
    assert result["created"]
    for key in result["created"]:
        r = s.db.scalar(select(Record).where(Record.kind == "material", Record.key == key))
        assert not r.published and not r.data.get("url") and not r.owner_id
    assert not l.prepare_content(s.db, s.ctx.admin)["created"]


def test_revoke_fp_role_cancels_reminder_before_delivery(learn):
    from ama.models import Role

    s = learn
    tr = assignment(s)
    sent(s, "learning-assignment:" + tr.id)
    s.clock[0] += timedelta(days=2)
    l.tick(s.db)
    row = s.db.get(Outbox, tr.data["reservation"])
    role = s.db.scalar(select(Role).where(Role.person_id == "fp"))
    role.active = False
    assert not l.deliverable(s.db, row)


def test_current_client_staff_role_does_not_expose_internal_role_library(learn):
    from ama.models import Role

    s = learn
    s.db.add(Role(person_id="fp", name="curator"))
    s.db.flush()
    r = mat(s, audience=["curator"])
    assert not l.can_material(s.db, s.actor, r)


def test_per_object_cc_material_does_not_inherit_other_object_service(learn):
    from ama.models import EstablishmentBrand, DigitalProgram, DigitalFact

    s = learn
    s.db.add(EstablishmentBrand(establishment_id="est", brand_id="brand"))
    s.db.add(Entity(id="other-est", kind="establishment", name="Второй адрес", partner_id="partner", data={}))
    s.db.flush()
    s.db.add(EstablishmentBrand(establishment_id="other-est", brand_id="brand"))
    s.db.flush()
    s.db.add(
        DigitalProgram(
            id="dp1", bot_id=l.settings().bot_id, establishment_id="est", work_id="work", data={"brands": ["brand"]}
        )
    )
    s.db.flush()
    s.db.add(
        DigitalFact(
            program_id="dp1",
            scope_key="common",
            field="call_center",
            data={"state": "known", "value": True, "confirmed": True},
        )
    )
    s.db.flush()
    r = mat(s, call_center_only=True)
    assert l.can_material(s.db, s.actor, r, establishment_id="est")
    assert not l.can_material(s.db, s.actor, r, establishment_id="other-est")


def test_parallel_learning_clicks_preserve_one_personal_result(learn):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    if learn.db.bind.dialect.name != "mysql":
        pytest.skip("Requires MySQL row locks")
    s = learn
    t = l.action(s.db, s.actor, "general")
    version = t.version
    s.db.commit()
    barrier = Barrier(2)

    def click(_):
        with s.ctx.factory.begin() as db:
            barrier.wait()
            try:
                l.action(db, s.actor, "general", "finish_module", version)
                return "ok"
            except Conflict:
                return "conflict"

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(click, range(2)))
    assert sorted(results) == ["conflict", "ok"]
    s.db.expire_all()
    t = l.selected_training(s.db, "fp", "general")
    assert t.position == 1 and t.acknowledged == [0]
