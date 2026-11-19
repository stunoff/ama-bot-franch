from datetime import datetime, timedelta
from types import SimpleNamespace
from hashlib import sha256
import pytest
from sqlalchemy import select, func
from ama import packets
from ama.auth import Actor, roles_for
from ama.models import (
    Packet,
    PacketObligation,
    PacketPublication,
    PacketAsset,
    PacketSchedule,
    PacketQuestion,
    Entity,
    EstablishmentBrand,
    Binding,
    Record,
    Role,
    Outbox,
    Work,
    SLACase,
)
from ama.errors import AccessDenied, BusinessError, Conflict
from ama.service import transition
from ama.ui import business, engine as ui, access, schema

BASE = datetime(2027, 1, 4, 9)


@pytest.fixture
def setup(ctx, monkeypatch):
    timebox = [BASE]
    monkeypatch.setattr(packets, "now", lambda: timebox[0])
    monkeypatch.setattr("ama.service.now", lambda: timebox[0])
    monkeypatch.setattr("ama.sla.now", lambda: timebox[0])
    partner = ctx.db.get(Entity, "partner")
    partner.data = {
        "notification_window": {
            "timezone": "Europe/Moscow",
            "weekdays": [0, 1, 2, 3, 4],
            "start": "11:00",
            "end": "20:00",
        },
        "partner_person_id": "fp",
        "curator_id": "curator",
    }
    ctx.db.get(Entity, "est").data = {
        "lifecycle": "operating",
        "city": "Москва",
        "format": "Кафе",
        "call_center_applicable": False,
    }
    ctx.db.add(EstablishmentBrand(establishment_id="est", brand_id="brand"))
    ctx.db.add(
        Record(
            kind="policy",
            key="packet-permission",
            published=True,
            data={
                "policy_type": "broadcast_permission",
                "person_id": "curator",
                "allow_general": True,
                "approval": "Назначено администратором",
            },
        )
    )
    ctx.db.add(
        Record(
            kind="policy",
            key="marketing-packet-permission",
            published=True,
            data={
                "policy_type": "broadcast_permission",
                "person_id": "marketing",
                "brand_ids": ["brand"],
                "allow_general": False,
                "approval": "Назначено администратором",
            },
        )
    )
    ctx.db.get(Binding, "support").topic_key = "10"
    ctx.db.get(Binding, "marketing").topic_key = "20"
    ctx.db.flush()
    return SimpleNamespace(ctx=ctx, db=ctx.db, clock=timebox, actor=ctx.curator)


def data(mode="acknowledgement", scope="partner_brand", general=False):
    return {
        "title": "Новые материалы",
        "text": "Заменить материалы к указанной дате.",
        "audience": "general" if general else "brands",
        "brand_ids": [] if general else ["brand"],
        "mode": mode,
        "scope": scope,
        "population": "all",
        "call_center": "all",
        "calendar": "support",
        "apply_at": (BASE + timedelta(days=1)).isoformat() + "Z",
        "due_at": (BASE + timedelta(days=10)).isoformat() + "Z",
        "remind_days": 2,
        "escalate_days": 5,
        "checklist": ["Проверить комплект", "Разместить материалы"],
        "evidence_mode": "text",
    }


def create(s, payload=None, key="a" * 32):
    result = packets.create(s.db, s.actor, payload or data(), key)
    p = packets.get(s.db, result["id"])
    return p, packets.revision(s.db, p)


def obs(s, r):
    return list(s.db.scalars(select(PacketObligation).where(PacketObligation.revision_id == r.id)))


def publications(s, r):
    return list(s.db.scalars(select(PacketPublication).where(PacketPublication.revision_id == r.id)))


def deliver(s, pub):
    for ident in pub.data["outbox_ids"]:
        row = s.db.get(Outbox, ident)
        row.status = "sent"
        packets.delivered(s.db, row)
    s.db.flush()


def add_brand(s, ident="brand2", binding="client2"):
    s.db.add(Entity(id=ident, kind="brand", name="Второй бренд", data={}))
    s.db.flush()
    s.db.add(EstablishmentBrand(establishment_id="est", brand_id=ident))
    s.db.add(
        Binding(
            id=binding,
            platform="telegram",
            chat_id=binding,
            purpose="client",
            partner_id="partner",
            brand_id=ident,
            active=True,
            verified=True,
            active_client_key="test-" + binding,
        )
    )
    s.db.flush()


def add_est(s, ident, brand="brand"):
    s.db.add(
        Entity(
            id=ident,
            kind="establishment",
            partner_id="partner",
            name="Кафе " + ident,
            data={"lifecycle": "operating", "city": "Москва", "format": "Кафе", "call_center_applicable": False},
        )
    )
    s.db.flush()
    s.db.add(EstablishmentBrand(establishment_id=ident, brand_id=brand))
    s.db.flush()


def test_admin_must_assign_publishing_permission(setup):
    s = setup
    with pytest.raises(AccessDenied):
        packets.publisher(s.db, s.ctx.admin)
    with pytest.raises(AccessDenied):
        packets.publisher(s.db, Actor("other", "support"))
    s.db.scalar(select(Record).where(Record.key == "packet-permission")).published = False
    with pytest.raises(AccessDenied):
        packets.audience(s.db, s.actor, data())


def test_information_one_publication_for_three_establishments_and_no_obligations(setup):
    s = setup
    add_est(s, "est2")
    add_est(s, "est3")
    p, r = create(s, data("information"))
    assert len(publications(s, r)) == 1 and len(publications(s, r)[0].data["items"]) == 3
    assert not obs(s, r) and not s.db.scalar(select(SLACase))
    packets.create(s.db, s.actor, data("information"), p.id)
    assert len(publications(s, r)) == 1


def test_three_establishments_have_three_separate_results(setup):
    s = setup
    add_est(s, "est2")
    add_est(s, "est3")
    p, r = create(s, data("task", "facility_brand"))
    assert len(publications(s, r)) == 1 and len(obs(s, r)) == 3
    assert len({o.work_id for o in obs(s, r)}) == 3
    assert all(s.db.get(Work, o.work_id).reviewer_id == "curator" for o in obs(s, r))


def test_common_acknowledgement_single_proof_across_two_brands(setup):
    s = setup
    add_brand(s)
    p, r = create(s, data(scope="partner", general=True))
    assert len(publications(s, r)) == 2 and len(obs(s, r)) == 1
    for pub in publications(s, r):
        deliver(s, pub)
    ob = obs(s, r)[0]
    result = packets.acknowledge(s.db, Actor("fp", "client2"), ob.id, ob.version, {})
    assert result["status"] == "acknowledged"
    assert packets.ob_visible(s.db, s.ctx.fp, ob)
    assert packets.acknowledge(s.db, s.ctx.fp, ob.id, 1, {})["status"] == "acknowledged"
    assert ob.data["source_binding"] == "client2"


def test_common_physical_task_is_one_work_with_two_client_presentations(setup):
    s = setup
    add_brand(s)
    p, r = create(s, data("task", "facility", True))
    ob = obs(s, r)[0]
    w = s.db.get(Work, ob.work_id)
    assert len(obs(s, r)) == 1 and w.brand_id is None
    assert "partner" in roles_for(s.db, Actor("fp", "client2"), w)
    for pub in publications(s, r):
        deliver(s, pub)
    packets.submit(
        s.db,
        Actor("fp", "client2"),
        ob.id,
        ob.version,
        {
            "completed_items": r.data["checklist"],
            "result_description": "Проверено и размещено",
            "work_version": w.version,
        },
        "shared-submit",
    )
    assert w.status == "review" and packets.status(s.db, ob) == "review"
    transition(s.db, s.ctx.curator, w.id, w.version, "accept", {}, "shared-accept")
    assert packets.status(s.db, ob) == "accepted" and packets.summary(s.db, p)["accepted"] == 1


def test_observer_cannot_ack_or_submit_and_unpublished_brand_cannot_act(setup):
    s = setup
    add_brand(s)
    p, r = create(s)
    deliver(s, publications(s, r)[0])
    ob = obs(s, r)[0]
    with pytest.raises(AccessDenied):
        packets.acknowledge(s.db, s.ctx.observer, ob.id, ob.version, {})
    with pytest.raises(AccessDenied):
        packets.acknowledge(s.db, Actor("fp", "client2"), ob.id, ob.version, {})


def test_missing_chat_and_unknown_filter_have_explicit_exclusions(setup):
    s = setup
    add_est(s, "unknown")
    s.db.get(Entity, "unknown").data = {"city": "Тверь"}
    snap = packets.audience(s.db, s.actor, {**data(), "population": "operating"})
    assert any(i["establishment_id"] == "unknown" and "Состояние" in i["reason"] for i in snap["excluded"])
    add_brand(s)
    s.db.get(Binding, "client2").active = False
    snap = packets.audience(s.db, s.actor, data(scope="partner", general=True))
    assert any(i["brand_id"] == "brand2" and "чата" in i["reason"] for i in snap["excluded"])


def test_preview_is_rolled_back_and_audience_change_requires_repreview(setup):
    s = setup
    draft = business.make_draft(s.db, s.actor, "packet:create", "Новый пакет", data())
    result = business.preview(s.db, s.actor, draft)
    assert not s.db.scalar(select(Packet)) and not s.db.scalar(select(Outbox))
    draft.status = "checked"
    draft.before = {
        "_checked": {"grants": access.grants_hash(s.db, s.actor), "packet_fingerprint": result["fingerprint"]}
    }
    add_est(s, "new-est")
    with pytest.raises(Conflict):
        business.confirm(s.db, s.actor, draft, draft.version)
    assert not s.db.scalar(select(Packet))


def test_material_scope_and_archived_content_are_not_published(setup):
    s = setup
    s.db.add(
        Record(
            kind="material",
            key="internal",
            published=True,
            data={"title": "Внутренний файл", "url": "https://example.org/internal", "audience": ["curator"]},
        )
    )
    s.db.flush()
    with pytest.raises(BusinessError, match="ФП"):
        packets.audience(s.db, s.actor, {**data(), "material_keys": ["internal"]})
    m = s.db.scalar(select(Record).where(Record.key == "internal"))
    m.data = {**m.data, "audience": ["partner"], "brand_id": "other-brand"}
    with pytest.raises(BusinessError, match="Материал"):
        packets.audience(s.db, s.actor, {**data(), "material_keys": ["internal"]})


def test_failed_part_retry_does_not_resend_sent_part(setup):
    s = setup
    p, r = create(s, data("information"))
    pub = publications(s, r)[0]
    original = s.db.get(Outbox, pub.data["outbox_ids"][0])
    original.status = "sent"
    extra = Outbox(
        dedupe_key="extra",
        binding_id="client",
        binding_version=1,
        platform="telegram",
        chat_id="client",
        status="failed",
        payload={
            "packet_publication": pub.id,
            "packet_revision": r.id,
            "packet_sender": original.payload["packet_sender"],
            "text": "Вторая часть",
        },
    )
    s.db.add(extra)
    s.db.flush()
    pub.data = {**pub.data, "outbox_ids": pub.data["outbox_ids"] + [extra.id]}
    result = packets.retry_publication(s.db, s.actor, pub.id, pub.version, {"reason": "Ошибка исправлена"})
    assert result["retried"] == 1 and original.status == "sent" and extra.status == "pending"
    extra.status = "uncertain"
    with pytest.raises(BusinessError, match="сверьте"):
        packets.retry_publication(s.db, s.actor, pub.id, pub.version, {"reason": "Неизвестный исход"})


def test_publication_is_delivered_only_after_all_parts(setup):
    s = setup
    p, r = create(s, data("information"))
    pub = publications(s, r)[0]
    row = s.db.get(Outbox, pub.data["outbox_ids"][0])
    other = Outbox(
        dedupe_key="part2",
        binding_id="client",
        binding_version=1,
        platform="telegram",
        chat_id="client",
        payload={"packet_publication": pub.id, "packet_revision": r.id, "text": "Часть 2"},
    )
    s.db.add(other)
    s.db.flush()
    pub.data = {**pub.data, "outbox_ids": pub.data["outbox_ids"] + [other.id]}
    row.status = "sent"
    packets.delivered(s.db, row)
    assert pub.delivered_at is None
    other.status = "sent"
    packets.delivered(s.db, other)
    assert pub.delivered_at


def test_task_submission_needs_checklist_and_explicit_named_fp(setup):
    s = setup
    p, r = create(s, data("task", "facility_brand"))
    ob = obs(s, r)[0]
    w = s.db.get(Work, ob.work_id)
    deliver(s, publications(s, r)[0])
    with pytest.raises(BusinessError, match="каждый"):
        packets.submit(
            s.db,
            s.ctx.fp,
            ob.id,
            ob.version,
            {"completed_items": [], "result_description": "Готово", "work_version": w.version},
            "bad-submit",
        )
    with pytest.raises(BusinessError, match="чек-лист"):
        transition(
            s.db, s.ctx.fp, w.id, w.version, "submit", {"_packet_submit": True, "result_description": "Обход"}, "forged"
        )
    assert w.status == "new"


def test_rework_updates_only_this_obligation_and_preserves_original_date(setup):
    s = setup
    add_est(s, "est2")
    p, r = create(s, data("task", "facility_brand"))
    ob = obs(s, r)[0]
    other = obs(s, r)[1]
    w = s.db.get(Work, ob.work_id)
    deliver(s, publications(s, r)[0])
    initial = w.original_due_at
    other_due = other.due_at
    packets.submit(
        s.db,
        s.ctx.fp,
        ob.id,
        ob.version,
        {"completed_items": r.data["checklist"], "result_description": "Готово", "work_version": w.version},
        "submit",
    )
    new_due = BASE + timedelta(days=15)
    transition(
        s.db,
        s.ctx.curator,
        w.id,
        w.version,
        "reject",
        {"reason": "Исправить размещение", "due_at": new_due.isoformat() + "Z"},
        "reject",
    )
    assert ob.due_at == new_due and other.due_at == other_due and w.original_due_at == initial
    assert packets.status(s.db, ob) == "rework"


def test_typo_keeps_existing_proof_and_revision_number(setup):
    s = setup
    p, r = create(s)
    deliver(s, publications(s, r)[0])
    ob = obs(s, r)[0]
    packets.acknowledge(s.db, s.ctx.fp, ob.id, ob.version, {})
    packets.typo(s.db, s.actor, p.id, p.version, {"text": "Исправлено написание слова.", "reason": "Опечатка"})
    assert p.current_revision == 1 and ob.status == "acknowledged" and r.data["typo_history"]
    assert s.db.scalar(select(Outbox).where(Outbox.dedupe_key.like("packet-typo:%")))


def test_substantial_revision_preserves_old_ack_and_requires_new_when_selected(setup):
    s = setup
    p, old = create(s)
    deliver(s, publications(s, old)[0])
    ob = obs(s, old)[0]
    packets.acknowledge(s.db, s.ctx.fp, ob.id, ob.version, {})
    packets.revise(
        s.db,
        s.actor,
        p.id,
        p.version,
        {
            "payload": {**data(), "text": "Новые существенные условия"},
            "reason": "Смена условий",
            "repeat_required": True,
            "cancel_pending": True,
        },
        "b" * 32,
    )
    new = packets.revision(s.db, p)
    assert p.current_revision == 2 and ob.status == "acknowledged" and obs(s, new)[0].status == "pending"
    assert not packets.deliverable(s.db, s.db.get(Outbox, publications(s, old)[0].data["outbox_ids"][0]))


def test_no_repeat_is_labelled_inherited_not_fake_ack_of_new_text(setup):
    s = setup
    p, r = create(s)
    deliver(s, publications(s, r)[0])
    ob = obs(s, r)[0]
    packets.acknowledge(s.db, s.ctx.fp, ob.id, ob.version, {})
    packets.revise(
        s.db,
        s.actor,
        p.id,
        p.version,
        {"payload": data(), "reason": "Автор решил не повторять", "repeat_required": False, "cancel_pending": True},
        "c" * 32,
    )
    inherited = obs(s, packets.revision(s.db, p))[0]
    assert inherited.status == "inherited" and inherited.data["inherited_from"] == ob.id
    assert not inherited.data.get("acknowledged_by")


def test_new_recipient_gets_only_active_packet_and_no_duplicate_prior_obligation(setup):
    s = setup
    p, r = create(s, data("task", "facility_brand"))
    deliver(s, publications(s, r)[0])
    old = obs(s, r)[0]
    add_est(s, "new-est")
    packets.add_recipients(
        s.db, s.actor, p.id, p.version, {"establishment_ids": ["new-est"], "reason": "Подключён новый объект"}
    )
    assert len(obs(s, r)) == 2 and obs(s, r)[0].id == old.id and len(publications(s, r)) == 1
    assert len(publications(s, r)[0].data["items"]) == 2
    assert any(
        s.db.get(Outbox, x).dedupe_key.startswith("packet-add:") for x in publications(s, r)[0].data["outbox_ids"]
    )
    s.clock[0] = BASE + timedelta(days=20)
    with pytest.raises(BusinessError, match="прошёл"):
        packets.add_recipients(s.db, s.actor, p.id, p.version, {"establishment_ids": ["new-est"], "reason": "Поздно"})


def test_transport_delay_is_not_fp_overdue_and_needs_agreed_future_deadline(setup):
    s = setup
    p, r = create(s, data("task", "facility_brand"))
    ob = obs(s, r)[0]
    initial = ob.due_at
    s.clock[0] = initial + timedelta(days=1)
    deliver(s, publications(s, r)[0])
    assert packets.status(s.db, ob) == "delivery_issue"
    packets.control_tick(s.db)
    assert not ob.data.get("overdue_signalled")
    packets.reschedule_obligation(
        s.db,
        s.ctx.curator,
        ob.id,
        ob.version,
        {"due_at": (s.clock[0] + timedelta(days=3)).isoformat() + "Z", "reason": "Пакет доставлен поздно"},
    )
    assert ob.due_at > initial and s.db.get(Work, ob.work_id).original_due_at == initial


def test_common_ack_reminder_reserved_once_and_cancelled_after_ack(setup):
    s = setup
    add_brand(s)
    p, r = create(s, data(scope="partner", general=True))
    ob = obs(s, r)[0]
    for pub in publications(s, r):
        deliver(s, pub)
    s.clock[0] = packets.Calendar(r.audience["calendar"]).shift_workdays(ob.due_at, 2) + timedelta(seconds=1)
    assert packets.control_tick(s.db)
    row = s.db.get(Outbox, ob.data["reminder_outbox"])
    assert row and row.payload["packet_reminder"] == ob.id
    s.clock[0] += timedelta(minutes=10)
    packets.control_tick(s.db)
    assert s.db.scalar(select(func.count()).select_from(Outbox).where(Outbox.dedupe_key.like("packet-reminder:%"))) == 1
    packets.acknowledge(s.db, s.ctx.fp, ob.id, ob.version, {})
    assert not packets.deliverable(s.db, row)


def test_publisher_revocation_before_send_blocks_delivery(setup):
    s = setup
    p, r = create(s, data("information"))
    row = s.db.get(Outbox, publications(s, r)[0].data["outbox_ids"][0])
    s.db.scalar(select(Role).where(Role.person_id == "curator")).active = False
    with pytest.raises((BusinessError, AccessDenied)):
        packets.deliverable(s.db, row)


def test_repeated_question_reuses_one_work_and_public_answer_is_separate(setup):
    s = setup
    p, r = create(s, data("information"))
    deliver(s, publications(s, r)[0])
    first = packets.question(
        s.db, s.ctx.fp, r.id, r.version, {"question": "Как заменить?", "establishment_id": "est"}, "q1"
    )
    second = packets.question(
        s.db, s.ctx.fp, r.id, r.version, {"question": "Уточнение", "establishment_id": "est"}, "q2"
    )
    assert first["id"] == second["id"] and s.db.scalar(select(func.count()).select_from(PacketQuestion)) == 1
    w = s.db.get(Work, first["id"])
    result = packets.answer(
        s.db,
        s.actor,
        w.id,
        w.version,
        {"text": "Замените по инструкции.", "next_check": (BASE + timedelta(days=2)).isoformat() + "Z"},
        "answer",
    )
    row = s.db.get(Outbox, result["id"])
    assert not w.data.get("public_answers")
    row.status = "sent"
    packets.delivered(s.db, row)
    assert w.data["public_answers"][0]["text"] == "Замените по инструкции."


def test_packet_assets_are_dedicated_and_hash_checked(setup, tmp_path, monkeypatch):
    s = setup
    from ama.config import settings

    monkeypatch.setattr(settings(), "file_root", str(tmp_path))
    draft = business.make_draft(s.db, s.actor, "packet:create", "Файлы", data("information"))
    file = tmp_path / "document.pdf"
    file.write_bytes(b"%PDF-public-packet")
    a = PacketAsset(
        author_id="curator",
        binding_id="support",
        draft_id=draft.id,
        platform="telegram",
        file_id="file",
        filename="document.pdf",
        source_key="asset1",
        status="stored",
        storage_path=str(file),
        sha256=sha256(file.read_bytes()).hexdigest(),
        size=file.stat().st_size,
    )
    s.db.add(a)
    s.db.flush()
    p, r = create(s, {**data("information"), "asset_ids": [a.id]})
    pub = publications(s, r)[0]
    assert len(pub.data["outbox_ids"]) == 2
    row = next(s.db.get(Outbox, x) for x in pub.data["outbox_ids"] if s.db.get(Outbox, x).payload.get("packet_asset"))
    assert packets.asset_path(s.db, row) == file
    file.write_bytes(b"changed")
    with pytest.raises(BusinessError, match="целостности"):
        packets.asset_path(s.db, row)


def test_native_ui_common_ack_is_scoped_and_callbacks_fit_telegram(setup):
    s = setup
    p, r = create(s)
    deliver(s, publications(s, r)[0])
    response = ui.dispatch(s.db, s.ctx.fp, ui.session(s.db, s.ctx.fp), "pk_view", {"revision_id": r.id})
    assert "Новые материалы" in response[0] and all(len(x.encode()) <= 64 for _, x in response[1])
    ob = obs(s, r)[0]
    response = ui.dispatch(s.db, s.ctx.fp, ui.session(s.db, s.ctx.fp), "pk_ack", {"obligation_id": ob.id})
    assert "Предпросмотр" in response[0]
    draft = s.db.scalar(
        select(__import__("ama.models", fromlist=["UIDraft"]).UIDraft).where(
            __import__("ama.models", fromlist=["UIDraft"]).UIDraft.kind == "obligation:ack"
        )
    )
    business.confirm(s.db, s.ctx.fp, draft, draft.version)
    assert ob.status == "acknowledged"


def test_schedule_clips_month_end_keeps_original_day_and_stops_at_end(setup):
    s = setup
    start = datetime(2027, 1, 31, 9)
    result = packets.schedule(
        s.db,
        s.actor,
        {
            "payload": data("information"),
            "start_at": start.isoformat() + "Z",
            "until_at": "2027-04-01T09:00:00Z",
            "repeat": "monthly",
        },
        "d" * 32,
    )
    schedule = s.db.get(PacketSchedule, result["id"])
    assert packets.next_cycle(schedule, start) == datetime(2027, 2, 28, 9)
    assert packets.next_cycle(schedule, datetime(2027, 2, 28, 9)) == datetime(2027, 3, 31, 9)
    s.clock[0] = start
    assert packets.schedule_tick(s.db)
    assert schedule.data["cycle"] == 1 and schedule.next_at == datetime(2027, 2, 28, 9)
    assert not packets.schedule_tick(s.db)
    assert s.db.scalar(select(func.count()).select_from(Packet)) == 1


def test_schedule_does_not_emit_archive_after_outage_or_revoked_author(setup):
    s = setup
    result = packets.schedule(
        s.db,
        s.actor,
        {
            "payload": data("information"),
            "start_at": "2027-01-10T09:00:00Z",
            "until_at": "2027-03-10T09:00:00Z",
            "repeat": "monthly",
        },
        "e" * 32,
    )
    schedule = s.db.get(PacketSchedule, result["id"])
    s.clock[0] = datetime(2027, 1, 20, 9)
    assert packets.schedule_tick(s.db)
    assert schedule.next_at == datetime(2027, 2, 10, 9) and not s.db.scalar(select(Packet))
    s.db.scalar(select(Role).where(Role.person_id == "curator")).version += 1
    s.clock[0] = schedule.next_at
    packets.schedule_tick(s.db)
    assert schedule.data["error"] and not s.db.scalar(select(Packet))


def test_long_publication_splits_utf16_without_duplicate_obligations(setup):
    s = setup
    p, r = create(
        s, {**data("task", "facility_brand"), "text": "🚀" * 2200, "checklist": ["Пункт " + str(i) for i in range(50)]}
    )
    pub = publications(s, r)[0]
    assert len(pub.data["outbox_ids"]) >= 2 and len(obs(s, r)) == 1
    assert all(
        len(s.db.get(Outbox, x).payload["text"].encode("utf-16-le")) // 2 <= 3500 for x in pub.data["outbox_ids"]
    )


def test_common_ack_cannot_be_multiplied_by_brand_option(setup):
    s = setup
    with pytest.raises(BusinessError, match="одно обязательство"):
        packets.audience(s.db, s.actor, data(scope="partner_brand", general=True))


def test_unassigned_account_cannot_browse_client_packets(setup):
    s = setup
    p, r = create(s)
    assert not packets.visible_items(s.db, Actor("consultant", "client"), r)
    with pytest.raises(AccessDenied):
        ui.dispatch(
            s.db,
            Actor("consultant", "client"),
            ui.session(s.db, Actor("consultant", "client")),
            "pk_view",
            {"revision_id": r.id},
        )


def test_multiple_object_reminders_are_grouped_and_only_open_items_survive_send(setup):
    s = setup
    add_est(s, "est2")
    add_est(s, "est3")
    p, r = create(s, data("task", "facility_brand"))
    deliver(s, publications(s, r)[0])
    s.clock[0] = packets.Calendar(r.audience["calendar"]).shift_workdays(obs(s, r)[0].due_at, 2) + timedelta(seconds=1)
    packets.control_tick(s.db)
    reminders = list(s.db.scalars(select(Outbox).where(Outbox.dedupe_key.like("packet-reminder:%"))))
    assert len(reminders) == 1 and len(reminders[0].payload["packet_reminder_ids"]) == 3
    one = obs(s, r)[0]
    one.status = "cancelled"
    assert packets.deliverable(s.db, reminders[0])
    assert one.id not in reminders[0].payload["packet_reminder_ids"]
    reminders[0].status = "sent"
    packets.delivered(s.db, reminders[0])
    assert all(o.data.get("last_reminder") for o in obs(s, r) if o.status != "cancelled")


def test_author_topic_created_once_and_delivery_resume_does_not_duplicate_it(setup):
    s = setup
    s.db.get(Binding, "support").topic_key = ""
    p, r = create(s, data("information"))
    row = s.db.scalar(select(Outbox).where(Outbox.dedupe_key == f"packet-topic:packet:{p.id}"))
    assert row and row.payload["packet_target"] == "packet"
    packets.on_topic(s.db, row, "4321")
    topic_id = p.topic_binding_id
    packets.on_topic(s.db, row, "4321")
    assert p.topic_binding_id == topic_id
    assert s.db.get(Binding, topic_id).topic_key == "4321"
    assert (
        s.db.scalar(select(func.count()).select_from(Outbox).where(Outbox.dedupe_key == f"packet-author:{p.id}")) == 1
    )


def test_sender_binding_replacement_keeps_obligation_and_delivery_proofs(setup):
    s = setup
    p, r = create(s)
    pub = publications(s, r)[0]
    deliver(s, pub)
    ob = obs(s, r)[0]
    packets.acknowledge(s.db, s.ctx.fp, ob.id, ob.version, {})
    s.db.add(
        Binding(
            id="replacement",
            platform="max",
            chat_id="replacement",
            purpose="client",
            partner_id="partner",
            brand_id="brand",
            active=True,
            verified=True,
        )
    )
    s.db.flush()
    new = s.db.get(Binding, "replacement")
    packets.replace_binding(s.db, s.db.get(Binding, "client"), new)
    s.db.get(Binding, "client").active = False
    assert packets.ob_visible(s.db, Actor("fp", "replacement"), ob)
    assert ob.status == "acknowledged" and pub.delivered_at and pub.binding_id == "replacement"
    assert packets.report(s.db, s.actor, p.id)[1][0][3] == "replacement"


def test_schedule_form_computes_cycle_dates_without_duplicate_required_dates(setup):
    fields = schema.fields_for("packet:schedule")
    payload = next(x for x in fields if x["key"] == "payload")
    assert not {"apply_at", "due_at"} & {x["key"] for x in payload["fields"]}
    s = setup
    payload_data = data("information")
    payload_data.pop("apply_at")
    payload_data.pop("due_at")
    draft = business.make_draft(
        s.db,
        s.actor,
        "packet:schedule",
        "Расписание",
        {
            "payload": payload_data,
            "start_at": "2027-01-10T09:00:00Z",
            "until_at": "2027-03-10T09:00:00Z",
            "repeat": "monthly",
        },
    )
    result = business.preview(s.db, s.actor, draft)
    assert result["fingerprint"] and not s.db.scalar(select(PacketSchedule))


def test_stopped_schedule_does_not_starve_valid_schedule(setup):
    s = setup
    for ident in ["1" * 32, "2" * 32]:
        packets.schedule(
            s.db,
            s.actor,
            {
                "payload": data("information"),
                "start_at": "2027-01-10T09:00:00Z",
                "until_at": "2027-03-10T09:00:00Z",
                "repeat": "monthly",
            },
            ident,
        )
    first = s.db.get(PacketSchedule, "1" * 32)
    first.enabled = False
    first.data = {**first.data, "error": "Требуется проверка"}
    s.clock[0] = datetime(2027, 1, 10, 9)
    assert packets.schedule_tick(s.db) and s.db.get(PacketSchedule, "2" * 32).data["cycle"] == 1


def test_scoped_curator_report_does_not_include_other_establishments(setup):
    s = setup
    add_est(s, "est2")
    p, r = create(s, data("task", "facility_brand"))
    s.db.scalar(select(Role).where(Role.person_id == "other", Role.name == "curator")).establishment_id = "est2"
    cols, rows = packets.report(s.db, Actor("other", "support"), p.id)
    obligation_rows = [row for row in rows if row[2] == "Обязательство"]
    assert len(obligation_rows) == 1 and "est2" in obligation_rows[0][3]


def test_long_titles_do_not_overflow_work_column(setup):
    s = setup
    p, r = create(s, {**data("task", "facility_brand"), "title": "А" * 250})
    assert len(s.db.get(Work, obs(s, r)[0].work_id).title) <= 250
    q = packets.question(
        s.db, s.ctx.fp, r.id, r.version, {"question": "Уточнение", "establishment_id": "est"}, "long-title-question"
    )
    assert len(s.db.get(Work, q["id"]).title) <= 250


def test_missing_support_route_is_recoverable_for_overdue_control(setup):
    s = setup
    p, r = create(s)
    deliver(s, publications(s, r)[0])
    ob = obs(s, r)[0]
    s.db.scalar(select(Record).where(Record.kind == "route", Record.key == "support")).published = False
    s.clock[0] = ob.due_at + timedelta(seconds=1)
    assert packets.control_tick(s.db) and ob.data["control_error"]
    assert not ob.data.get("overdue_signalled")


def test_packet_report_preview_stays_scoped_and_creates_no_delivery(setup):
    s = setup
    p, r = create(s)
    before = s.db.scalar(select(func.count()).select_from(Outbox))
    draft = business.make_draft(s.db, s.actor, "packet:report", "Отчёт", {}, target=p)
    result = business.preview(s.db, s.actor, draft)
    assert result["rows"] == 2
    assert s.db.scalar(select(func.count()).select_from(Outbox)) == before


def test_mysql_concurrent_common_ack_creates_one_proof(setup):
    s = setup
    if s.db.bind.dialect.name != "mysql":
        pytest.skip("MySQL row locks required")
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    add_brand(s)
    p, r = create(s, data(scope="partner", general=True))
    for pub in publications(s, r):
        deliver(s, pub)
    ob = obs(s, r)[0]
    ident = ob.id
    expected = ob.version
    s.db.commit()
    barrier = Barrier(2)

    def acknowledge(binding):
        with s.ctx.factory.begin() as db:
            barrier.wait(timeout=10)
            return packets.acknowledge(db, Actor("fp", binding), ident, expected, {})

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(acknowledge, ["client", "client2"]))
    s.db.expire_all()
    assert all(row["status"] == "acknowledged" for row in results)
    current = s.db.get(PacketObligation, ident)
    assert current.version == expected + 1
    from ama.models import Audit

    assert (
        s.db.scalar(
            select(func.count())
            .select_from(Audit)
            .where(Audit.object_id == ident, Audit.action == "PACKET_ACKNOWLEDGED")
        )
        == 1
    )


def test_mysql_concurrent_deliveries_do_not_reset_common_anchor(setup):
    s = setup
    if s.db.bind.dialect.name != "mysql":
        pytest.skip("MySQL row locks required")
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    add_brand(s)
    p, r = create(s, data(scope="partner", general=True))
    pubs = publications(s, r)
    ident = obs(s, r)[0].id
    outbox_ids = [pub.data["outbox_ids"][0] for pub in pubs]
    s.db.commit()
    barrier = Barrier(2)

    def confirm(outbox_id):
        with s.ctx.factory.begin() as db:
            row = db.get(Outbox, outbox_id)
            row.status = "sent"
            barrier.wait(timeout=10)
            packets.delivered(db, row)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(confirm, outbox_ids))
    s.db.expire_all()
    ob = s.db.get(PacketObligation, ident)
    first = ob.data["delivered_publication"]
    assert first in {pub.id for pub in pubs} and ob.data["delivered_at"]
    for pub in publications(s, r):
        deliver(s, pub)
    assert ob.data["delivered_publication"] == first


def test_shared_ack_through_native_max_callback(setup):
    s = setup
    add_brand(s)
    from ama.models import Account
    from ama import bot

    b = s.db.get(Binding, "client2")
    b.platform = "max"
    b.chat_id = "200"
    s.db.add(Account(person_id="fp", platform="max", external_id="100"))
    s.db.flush()
    p, r = create(s, data(scope="partner", general=True))
    for pub in publications(s, r):
        deliver(s, pub)
    ob = obs(s, r)[0]
    actor = Actor("fp", "client2")
    response = ui.dispatch(s.db, actor, ui.session(s.db, actor), "pk_ack", {"obligation_id": ob.id})
    callback = next(payload for label, payload in response[1] if "Подтвердить применение" in label)
    bot.handle(
        s.db,
        "max",
        {
            "update_type": "message_callback",
            "callback": {"callback_id": "max-ack", "user": {"user_id": 100}, "payload": callback},
            "message": {"recipient": {"chat_id": 200}},
        },
    )
    assert ob.status == "acknowledged" and ob.data["source_binding"] == "client2"
    assert packets.ob_visible(s.db, s.ctx.fp, ob)


def test_uploaded_schedule_files_are_saved_inside_template(setup):
    s = setup
    draft = business.make_draft(s.db, s.actor, "packet:schedule", "Пакет", {"payload": data("information")})
    screen = ui.session(s.db, s.actor)
    screen.state = {
        "prompt": {"type": "packet_asset", "draft_id": draft.id, "expires": (BASE + timedelta(minutes=15)).isoformat()}
    }
    event = SimpleNamespace(
        files=[{"file_id": "f", "filename": "public.pdf"}], platform="telegram", key="nested-upload"
    )
    assert packets.asset_receive(s.db, s.actor, event)
    assert len(draft.data["payload"]["asset_ids"]) == 1 and "asset_ids" not in {k for k in draft.data if k != "payload"}


def test_multipart_typo_replaces_text_manifest_without_duplicate_obligations(setup):
    s = setup
    p, r = create(s, {**data("task", "facility_brand"), "text": "🚀" * 2200})
    pub = publications(s, r)[0]
    old_parts = list(pub.data["outbox_ids"])
    deliver(s, pub)
    packets.typo(s.db, s.actor, p.id, p.version, {"text": "🚀" * 2400, "reason": "Исправлена опечатка"})
    assert len(obs(s, r)) == 1 and not set(old_parts) & set(pub.data["outbox_ids"])
    assert pub.data["retired_parts"] and not pub.delivered_at
    assert all(
        len(s.db.get(Outbox, x).payload["text"].encode("utf-16-le")) // 2 <= 3500 for x in pub.data["outbox_ids"]
    )
    deliver(s, pub)
    assert pub.delivered_at and all(s.db.get(Outbox, x).status == "sent" for x in old_parts)


def test_weekly_schedule_preserves_local_hour_across_dst(setup):
    s = setup
    cal = s.db.scalar(select(Record).where(Record.kind == "calendar", Record.key == "support"))
    cal.data = {**cal.data, "timezone": "Europe/Berlin"}
    result = packets.schedule(
        s.db,
        s.actor,
        {
            "payload": data("information"),
            "start_at": "2027-03-26T11:00:00+01:00",
            "until_at": "2027-04-30T12:00:00Z",
            "repeat": "weekly",
        },
        "f" * 32,
    )
    plan = s.db.get(PacketSchedule, result["id"])
    assert packets.next_cycle(plan, datetime(2027, 3, 26, 10)) == datetime(2027, 4, 2, 9)


def test_existing_project_is_operating_without_a_fictional_opening_date(setup):
    s = setup
    from ama.models import ProjectPlan

    s.db.add(
        ProjectPlan(
            id="existing",
            bot_id="ama-franchising",
            title="Действующее",
            partner_id="partner",
            establishment_id="est",
            brand_id="brand",
            curator_id="curator",
            scenario="existing",
            profile={},
            dates={},
        )
    )
    s.db.get(Entity, "est").data = {**s.db.get(Entity, "est").data, "lifecycle": "unknown"}
    s.db.flush()
    snap = packets.audience(s.db, s.actor, {**data("information"), "population": "operating"})
    assert len(snap["included"]) == 1 and s.db.get(ProjectPlan, "existing").dates == {}
