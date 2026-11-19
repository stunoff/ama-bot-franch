"""Temporary requirement tests for independent audit; expected failures expose 0.10.0 gaps."""

from datetime import timedelta
from hashlib import sha256
from sqlalchemy import select, func
import pytest
from ama.auth import Actor
from ama import service, worker, bot, wizard, finance
from ama.models import Work, Outbox, Attachment, Record, Dialog, now
from ama.errors import AccessDenied
from test_finance import fin, BASE  # noqa: F401 -- pytest fixture


def test_staff_business_action_must_be_rejected_in_client_chat(ctx):
    ctx.w.status = "review"
    ctx.w.owner_id = ctx.w.reviewer_id = "curator"
    with pytest.raises(AccessDenied):
        service.transition(
            ctx.db, Actor("curator", "client"), ctx.w.id, ctx.w.version, "accept", {}, "audit-cross-chat"
        )


def test_design_result_requires_marketing_manager_not_curator(ctx):
    ctx.w.kind = "design"
    ctx.w.status = "review"
    ctx.w.owner_id = ctx.w.reviewer_id = "curator"
    with pytest.raises(AccessDenied):
        service.transition(ctx.db, ctx.curator, ctx.w.id, ctx.w.version, "accept", {}, "audit-design-review")


def test_marketing_request_remains_open_until_fp_confirms(ctx):
    ctx.db.add(
        Record(
            kind="route",
            key="marketing",
            published=True,
            data={
                "binding_id": "marketing",
                "owner_id": "marketing",
                "reviewer_id": "marketing",
                "calendar": "support",
            },
        )
    )
    ctx.db.flush()
    w = service.create_work(
        ctx.db,
        ctx.fp,
        {"kind": "marketing", "title": "Собственная проблема ФП", "establishment_id": None},
        "audit-marketing-request",
    )
    w.status = "review"
    w.owner_id = w.reviewer_id
    service.transition(ctx.db, Actor("marketing", "marketing"), w.id, w.version, "accept", {}, "audit-marketing-accept")
    assert w.status == "review" and w.reviewer_id == "fp" and w.outcome is None


def test_consultant_can_use_declared_finance_request_flow(fin):  # noqa: F811
    s = fin
    a = finance.request(
        s.db,
        s.ctx.consultant,
        {
            "partner_id": "partner",
            "brand_id": "brand",
            "establishment_id": "est",
            "party_details": "Подтверждённые реквизиты",
            "terms": "Условия",
            "basis": "Основание",
            "due_at": (BASE + timedelta(days=10)).isoformat() + "Z",
        },
        "audit-consultant-contract",
    )
    assert a.data["communicator_id"] == "consultant"


def test_photo_reply_to_work_card_resolves_original_work(ctx):
    ctx.db.add(
        Outbox(
            dedupe_key="audit-original-card",
            binding_id="client",
            binding_version=1,
            platform="telegram",
            chat_id="client",
            payload={"text": "Карточка\nID: work"},
            work_id="work",
            status="sent",
            external_id="777",
        )
    )
    ctx.db.flush()
    bot.handle(
        ctx.db,
        "telegram",
        {
            "update_id": 987654,
            "message": {
                "message_id": 7778,
                "from": {"id": "fp"},
                "chat": {"id": "client", "type": "group"},
                "reply_to_message": {
                    "message_id": 777,
                    "from": {"id": "bot", "is_bot": True},
                    "text": "Карточка\nID: work",
                },
                "photo": [{"file_id": "audit-photo"}],
            },
        },
    )
    assert ctx.db.scalar(select(func.count()).select_from(Attachment).where(Attachment.work_id == "work")) == 1


def test_new_attachment_is_delivered_to_current_reviewer_queue(ctx, tmp_path, monkeypatch):
    ctx.w.binding_id = "design"
    ctx.w.review_binding_id = "support"
    ctx.w.status = "review"
    ctx.w.owner_id = ctx.w.reviewer_id = "curator"
    path = tmp_path / "file.txt"
    path.write_text("proof")
    a = Attachment(
        source_key="audit-attachment",
        work_id="work",
        author_id="fp",
        source_binding_id="client",
        platform="telegram",
        file_id="audit",
        filename="file.txt",
        status="pending",
    )
    ctx.db.add(a)
    ctx.db.commit()
    monkeypatch.setattr(worker, "Session", ctx.factory)
    monkeypatch.setattr(worker, "download", lambda a, c: (str(path), 5, sha256(b"proof").hexdigest()))
    assert worker.attachment_step(type("Transport", (), {"client": None})())
    ctx.db.expire_all()
    row = ctx.db.scalar(select(Outbox).where(Outbox.dedupe_key == "attachment:" + a.id))
    assert row.binding_id == "support"


def test_request_preview_supports_files_and_edit_before_submit(ctx):
    form = Dialog(
        person_id="fp",
        binding_id="client",
        stage="preview",
        data={"branch": "support", "category": 0, "answers": []},
        version=1,
    )
    ctx.db.add(form)
    ctx.db.flush()
    _, choices = wizard.options(ctx.db, ctx.fp, form)
    labels = [item["text"] for item in choices]
    assert "Добавить фото/файл" in labels and "Изменить" in labels


def test_legacy_debt_cannot_spend_one_bank_operation_twice(ctx):
    from ama.operations import operation
    from ama.errors import BusinessError

    debts = []
    for index in range(2):
        w = Work(
            kind="debt",
            title="Старый / совместимый долг " + str(index),
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
        debts.append(w)
        operation(
            ctx.db, ctx.contract, w.id, w.version, "invoice", {"amount_minor": 10000, "document": "Проверенный счёт"}
        )
    operation(
        ctx.db,
        ctx.contract,
        debts[0].id,
        debts[0].version,
        "debt_payment",
        {"amount_minor": 10000, "bank_reference": "same-actual-bank-wire"},
    )
    with pytest.raises(BusinessError):
        operation(
            ctx.db,
            ctx.contract,
            debts[1].id,
            debts[1].version,
            "debt_payment",
            {"amount_minor": 10000, "bank_reference": "same-actual-bank-wire"},
        )
