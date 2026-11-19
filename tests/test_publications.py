from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
import pytest
from sqlalchemy import select, func
from ama import publications as p, worker
from ama.config import settings
from ama.errors import AccessDenied, BusinessError, Conflict
from ama.models import Attachment, ClientPublication, Outbox, EstablishmentBrand, Role, now
from ama.ui import business, engine, controller


@pytest.fixture
def pub(ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(settings(), "file_root", str(tmp_path))
    ctx.db.add(EstablishmentBrand(establishment_id="est", brand_id="brand"))
    assets = []
    for i in range(2):
        content = ("private-file-" + str(i)).encode()
        path = tmp_path / (str(i) + ".txt")
        path.write_bytes(content)
        a = Attachment(
            source_key="asset:" + str(i),
            work_id=ctx.w.id,
            author_id="curator",
            source_binding_id="support",
            platform="telegram",
            file_id="private",
            filename="draft-" + str(i) + ".txt",
            storage_path=str(path),
            size=len(content),
            sha256=sha256(content).hexdigest(),
            status="stored",
            visibility="internal",
        )
        ctx.db.add(a)
        assets.append(a)
    ctx.db.flush()
    return SimpleNamespace(ctx=ctx, db=ctx.db, assets=assets, actor=ctx.curator)


def draft(s, ids=None, **data):
    return business.make_draft(
        s.db,
        s.actor,
        "publication:send",
        "Отправка",
        {
            "mode": "reply",
            "text": "Разрешённый текст для ФП",
            "attachment_ids": ids if ids is not None else [s.assets[0].id],
            **data,
        },
        s.ctx.w,
    )


def queued(s, d=None):
    d = d or draft(s)
    p.prepare(s.db, s.actor, d)
    result = p.send(s.db, s.actor, d)
    s.db.flush()
    obj = s.db.get(ClientPublication, result["id"])
    return obj, [s.db.get(Outbox, k) for k in obj.data["parts"]]


def test_preview_rolls_back_publication_and_selected_files_remain_internal(pub):
    s = pub
    d = draft(s)
    p.prepare(s.db, s.actor, d)
    result = business.preview(s.db, s.actor, d)
    assert result["Выбранные файлы"] == [s.assets[0].filename]
    assert s.db.scalar(select(func.count()).select_from(ClientPublication)) == 0
    assert s.db.scalar(select(func.count()).select_from(Outbox)) == 0
    assert all(a.visibility == "internal" for a in s.assets)


def test_only_selected_files_are_queued_and_repeat_is_idempotent(pub):
    s = pub
    d = draft(s)
    obj, rows = queued(s, d)
    assert len(rows) == 2 and rows[1].payload["attachment_id"] == s.assets[0].id
    assert all(r.binding_id == "client" for r in rows)
    assert p.send(s.db, s.actor, d)["id"] == obj.id
    assert s.db.scalar(select(func.count()).select_from(ClientPublication)) == 1
    assert all(a.visibility == "internal" for a in s.assets)


@pytest.mark.parametrize("actor", ["admin", "fp", "observer", "specialist"])
def test_non_business_or_unassigned_roles_cannot_publish(pub, actor):
    with pytest.raises(AccessDenied):
        p.authorize(pub.db, getattr(pub.ctx, actor), pub.ctx.w)


def test_other_work_file_cannot_be_selected(pub):
    from ama.models import Work

    s = pub
    other = Work(
        id="other-work",
        kind="support",
        title="Другой пункт",
        partner_id="partner",
        brand_id="brand",
        establishment_id="est",
        owner_id="fp",
        executor_id="fp",
        reviewer_id="curator",
        binding_id="support",
        due_at=now() + timedelta(days=2),
        original_due_at=now() + timedelta(days=2),
        data={},
    )
    s.db.add(other)
    s.db.flush()
    s.assets[0].work_id = "other-work"
    s.db.flush()
    with pytest.raises(AccessDenied):
        p.prepare(s.db, s.actor, draft(s))


def test_file_modified_after_preview_is_rejected(pub):
    s = pub
    d = draft(s)
    p.prepare(s.db, s.actor, d)
    Path(s.assets[0].storage_path).write_bytes(b"replaced-content")
    with pytest.raises(BusinessError, match="изменилось"):
        p.send(s.db, s.actor, d)


def test_file_symlink_and_outside_storage_are_rejected(pub, tmp_path):
    s = pub
    outside = tmp_path.parent / (tmp_path.name + "-outside.txt")
    outside.write_text("secret")
    try:
        s.assets[0].storage_path = str(outside)
        with pytest.raises(BusinessError):
            p.prepare(s.db, s.actor, draft(s))
        link = tmp_path / "link.txt"
        link.symlink_to(outside)
        s.assets[0].storage_path = str(link)
        with pytest.raises(BusinessError):
            p.prepare(s.db, s.actor, draft(s))
    finally:
        outside.unlink()


def test_changed_recipient_or_work_requires_new_preview(pub):
    s = pub
    d = draft(s)
    p.prepare(s.db, s.actor, d)
    from ama.models import Binding

    b = s.db.get(Binding, "client")
    b.version += 1
    with pytest.raises(Conflict):
        p.send(s.db, s.actor, d)


def test_expired_preview_cannot_send(pub, monkeypatch):
    s = pub
    d = draft(s)
    p.prepare(s.db, s.actor, d)
    monkeypatch.setattr(p, "now", lambda: now() + timedelta(minutes=16))
    with pytest.raises(BusinessError, match="истёк"):
        p.send(s.db, s.actor, d)


def test_ownership_change_cancels_before_external_delivery(pub):
    s = pub
    obj, rows = queued(s)
    s.ctx.w.owner_id = s.ctx.w.reviewer_id = "other"
    assert p.deliverable(s.db, rows[0]) is False


def test_uncertain_text_reserves_files_without_blind_retry(pub):
    s = pub
    obj, rows = queued(s)
    assert p.deliverable(s.db, rows[1]) is None
    rows[0].status = "uncertain"
    p.refresh(s.db, rows[0])
    assert obj.status == "uncertain" and p.deliverable(s.db, rows[1]) is None
    rows[0].status = "sent"
    p.refresh(s.db, rows[0])
    assert p.deliverable(s.db, rows[1]) is True
    rows[1].status = "failed"
    p.refresh(s.db, rows[1])
    assert obj.status == "partial"
    rows[1].status = "sent"
    p.refresh(s.db, rows[1])
    assert obj.status == "sent"
    assert not s.ctx.w.status == "done"


def test_revoked_role_blocks_confirmed_publication(pub):
    s = pub
    obj, rows = queued(s)
    role = s.db.scalar(select(Role).where(Role.person_id == "curator", Role.name == "curator"))
    role.active = False
    assert p.deliverable(s.db, rows[0]) is False


def test_unapproved_internal_attachment_cannot_use_generic_client_queue(pub):
    from ama.service import queue
    from ama.models import Binding

    s = pub
    r = queue(s.db, s.db.get(Binding, "client"), "forged", "internal", work=s.ctx.w, attachment_id=s.assets[1].id)
    assert p.deliverable(s.db, r) is False


def test_client_cannot_open_internal_file_browser(pub):
    s = pub
    from ama.ui.workspace import dispatch

    screen = engine.session(s.db, s.ctx.fp)
    with pytest.raises(AccessDenied):
        dispatch(s.db, s.ctx.fp, screen, "work_files", {"work_id": s.ctx.w.id})


def test_cancel_leaves_sent_parts_as_history(pub):
    s = pub
    obj, rows = queued(s)
    rows[0].status = "sent"
    p.cancel(s.db, s.actor, obj.id, obj.version)
    assert obj.status == "cancelled" and rows[0].status == "sent" and rows[1].status == "cancelled"


def test_worker_sends_explicit_publication_but_not_unselected_file(pub, monkeypatch):
    s = pub
    obj, rows = queued(s)
    # The agreed client window is all week in UTC for this transport test.
    from ama.models import Entity

    partner = s.db.get(Entity, "partner")
    partner.data = {
        "notification_window": {"timezone": "UTC", "weekdays": list(range(7)), "start": "00:00", "end": "23:59"}
    }
    s.db.commit()
    monkeypatch.setattr(worker, "Session", s.ctx.factory)
    sent = []
    transport = SimpleNamespace(send=lambda *args: (sent.append(args) or "remote-id"))
    assert worker.outbox_step(transport)
    assert worker.outbox_step(transport)
    assert len(sent) == 2 and sent[1][-1] == Path(s.assets[0].storage_path)
    s.db.expire_all()
    assert s.db.get(ClientPublication, obj.id).status == "sent"


def test_request_data_timer_waits_for_complete_delivered_selection(pub):
    from ama.sla import initialize, case_for

    s = pub
    s.ctx.w.owner_id = s.ctx.w.executor_id = "curator"
    initialize(s.db, s.ctx.w)
    d = draft(
        s, mode="request", reason="Нужны конкретные вводные", next_check=(now() + timedelta(days=4)).isoformat() + "Z"
    )
    obj, rows = queued(s, d)
    assert s.ctx.w.status == "waiting_partner" and len(rows) == 2
    c = case_for(s.db, s.ctx.w)
    assert c.data["request"]["delivered_at"] is None
    rows[0].status = "sent"
    p.refresh(s.db, rows[0])
    assert c.data["request"]["delivered_at"] is None
    rows[1].status = "sent"
    p.refresh(s.db, rows[1])
    assert c.data["request"]["delivered_at"]


def test_actual_preview_and_ui_callbacks(pub):
    s = pub
    screen = engine.session(s.db, s.actor)
    text, buttons = controller.dispatch(s.db, s.actor, screen, "pub_new", {"work_id": s.ctx.w.id})
    assert "Отправка" in text and all(len(b[1].encode()) <= 64 for b in buttons)
    d = draft(s)
    text, buttons = controller.dispatch(s.db, s.actor, screen, "preview", {"draft_id": d.id})
    assert d.status == "checked"
    assert "ФП:" in text and "Бренд:" in text and "Предприятие:" in text and "Адресат:" in text
    assert "_snapshot" not in text and "_digest" not in text
    full_text, _ = controller.dispatch(s.db, s.actor, screen, "content_preview", {"draft_id": d.id})
    assert "_snapshot" not in full_text and "_digest" not in full_text
    result = business.confirm(s.db, s.actor, d, d.version)
    assert result["files"] if "files" in result else result["Выбранные файлы"] == [s.assets[0].filename]


def test_same_confirmed_publication_is_not_duplicated_by_parallel_workers(pub):
    if pub.db.bind.dialect.name != "mysql":
        pytest.skip("Requires MySQL row locks")
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from ama.models import UIDraft

    s = pub
    d = draft(s)
    p.prepare(s.db, s.actor, d)
    ident = d.id
    s.db.commit()
    barrier = Barrier(2)

    def submit(_):
        with s.ctx.factory.begin() as db:
            local = db.get(UIDraft, ident)
            barrier.wait()
            return p.send(db, s.actor, local)["id"]

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(submit, range(2)))
    assert results[0] == results[1]
    s.db.expire_all()
    assert s.db.scalar(select(func.count()).select_from(ClientPublication)) == 1
    assert s.db.scalar(select(func.count()).select_from(Outbox)) == 2
