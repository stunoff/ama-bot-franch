"""Upgrade a populated 0.9 database; no automatic conversion of historical facts."""

from datetime import datetime
from subprocess import run
import os
import sys
from sqlalchemy import create_engine, MetaData, select
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from ama.db import Base


def test_0009_to_0010_preserves_finance_learning_and_business_proofs(tmp_path):
    url = "sqlite:///" + str(tmp_path / "digital-upgrade.db")
    engine = create_engine(url)
    env = {**os.environ, "DATABASE_URL": url}

    def upgrade(revision):
        proc = run([sys.executable, "-m", "alembic", "upgrade", revision], env=env, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr

    upgrade("0009")
    meta = MetaData()
    meta.reflect(engine)
    at = datetime(2026, 11, 17, 12, 34, 56, 123456)
    with engine.begin() as c:
        c.execute(meta.tables["people"].insert(), {"id": "curator", "name": "Куратор", "version": 1, "created_at": at})
        c.execute(
            meta.tables["entities"].insert(),
            [
                {"id": "partner", "kind": "partner", "name": "ФП", "version": 1, "data": {}},
                {
                    "id": "est",
                    "kind": "establishment",
                    "name": "Кафе",
                    "partner_id": "partner",
                    "version": 1,
                    "data": {},
                },
            ],
        )
        c.execute(
            meta.tables["chat_bindings"].insert(),
            {
                "id": "support",
                "bot_id": "ama",
                "platform": "telegram",
                "chat_id": "123",
                "topic_key": "",
                "purpose": "support",
                "active": True,
                "verified": True,
                "version": 1,
            },
        )
        old = {
            "passport": {
                "management_messenger": "max",
                "work_phone_messenger": "telegram",
                "call_center_applicable": False,
            },
            "components": {"ama_rs": {"confirmed": True, "control_test": "Оба направления проверены"}},
            "ledger": [{"amount_minor": 123456}],
        }
        c.execute(
            meta.tables["works"].insert(),
            {
                "id": "legacy",
                "kind": "digital",
                "title": "Принятый цифровой результат",
                "status": "done",
                "outcome": "accepted",
                "priority": "normal",
                "partner_id": "partner",
                "establishment_id": "est",
                "owner_id": "curator",
                "executor_id": "curator",
                "reviewer_id": "curator",
                "binding_id": "support",
                "due_at": at,
                "original_due_at": at,
                "version": 8,
                "returns": 2,
                "data": old,
                "created_at": at,
            },
        )
        c.execute(
            meta.tables["packets"].insert(),
            {
                "id": "packet",
                "bot_id": "ama",
                "author_id": "curator",
                "source_binding_id": "support",
                "title": "Действующая рассылка",
                "current_revision": 1,
                "version": 3,
                "created_at": at,
            },
        )
        c.execute(
            meta.tables["packet_revisions"].insert(),
            {
                "id": "revision",
                "packet_id": "packet",
                "number": 1,
                "version": 2,
                "data": {"text": "Подтверждённый текст"},
                "audience": {"included": []},
                "created_at": at,
            },
        )
        c.execute(
            meta.tables["packet_obligations"].insert(),
            {
                "id": "proof",
                "revision_id": "revision",
                "scope_key": "partner",
                "partner_id": "partner",
                "person_id": "curator",
                "curator_id": "curator",
                "binding_id": "support",
                "status": "acknowledged",
                "due_at": at,
                "next_at": at,
                "version": 7,
                "data": {"ack_actor": "curator", "ack_at": at.isoformat(), "delivered_at": at.isoformat()},
            },
        )
    with engine.begin() as c:
        c.execute(
            meta.tables["configuration"].insert(),
            {
                "id": "course",
                "kind": "course",
                "key": "general",
                "version": 3,
                "published": True,
                "data": {"cards": [{"title": "Карточка", "text": "Утверждённый текст", "required_materials": []}]},
            },
        )
        c.execute(
            meta.tables["training"].insert(),
            {
                "id": "old-course",
                "person_id": "curator",
                "course_key": "general",
                "course_version": 3,
                "cards": [{"title": "Карточка", "text": "Утверждённый текст", "required_materials": []}],
                "position": 0,
                "acknowledged": [0],
                "completed_at": at,
                "paused": False,
                "version": 7,
            },
        )
    with engine.begin() as c:
        c.execute(
            meta.tables["learning_tracks"].insert(),
            {
                "id": "track",
                "bot_id": "ama",
                "person_id": "curator",
                "course_key": "general",
                "training_id": "old-course",
                "binding_id": "support",
                "origin_key": "immutable-progress",
                "kind": "general",
                "status": "done",
                "version": 9,
                "data": {"module_confirmations": [0], "completed_by": "curator", "completed_at": at.isoformat()},
            },
        )
        c.execute(
            meta.tables["learning_editions"].insert(),
            {
                "id": "edition",
                "record_id": "course",
                "record_version": 3,
                "created_at": at,
                "data": {"immutable": True, "cards": ["Approved"]},
            },
        )
    with engine.begin() as c:
        c.execute(
            meta.tables["finance_agreements"].insert(),
            {
                "id": "agreement",
                "bot_id": "ama",
                "work_id": "legacy",
                "partner_id": "partner",
                "version": 4,
                "active": True,
                "data": {"signed_package": 2, "signatures": {"uk": "proof", "partner": "proof"}},
            },
        )
        c.execute(
            meta.tables["finance_obligations"].insert(),
            {
                "id": "obligation",
                "agreement_id": "agreement",
                "period": "2026-11",
                "amount_minor": 5000,
                "due_at": at,
                "version": 6,
                "data": {"invoice": {"number": "INV-1", "status": "sent"}, "original_due": at.isoformat()},
            },
        )
        c.execute(
            meta.tables["finance_receipts"].insert(),
            {
                "id": "bank",
                "bot_id": "ama",
                "recipient_key": "approved-recipient",
                "bank_reference": "unique-bank-id",
                "amount_minor": 1000,
                "received_at": at,
                "confirmed_by": "curator",
                "version": 3,
                "data": {"bank_proof": "verified"},
            },
        )
        c.execute(
            meta.tables["finance_allocations"].insert(),
            {
                "id": "allocation",
                "receipt_id": "bank",
                "obligation_id": "obligation",
                "amount_minor": 1000,
                "source_key": "allocation-original",
                "created_at": at,
                "data": {"confirmed": "actual"},
            },
        )
    upgrade("head")
    upgrade("head")
    meta.reflect(engine)
    with engine.connect() as c:
        w = c.execute(select(meta.tables["works"]).where(meta.tables["works"].c.id == "legacy")).mappings().one()
        assert (
            w["data"] == old
            and w["version"] == 8
            and w["returns"] == 2
            and w["original_due_at"] == at
            and w["outcome"] == "accepted"
        )
        proof = c.execute(select(meta.tables["packet_obligations"])).mappings().one()
        assert proof["version"] == 7 and proof["status"] == "acknowledged" and proof["data"]["ack_at"] == at.isoformat()
        assert not c.execute(select(meta.tables["digital_programs"])).first()
        assert not c.execute(select(meta.tables["digital_facts"])).first()
        old_training = c.execute(select(meta.tables["training"])).mappings().one()
        assert (
            old_training["version"] == 7 and old_training["completed_at"] == at and old_training["acknowledged"] == [0]
        )
        track = c.execute(select(meta.tables["learning_tracks"])).mappings().one()
        assert track["status"] == "done" and track["version"] == 9 and track["data"]["completed_at"] == at.isoformat()
        edition = c.execute(select(meta.tables["learning_editions"])).mappings().one()
        assert edition["record_version"] == 3 and edition["created_at"] == at and edition["data"]["immutable"]
        assert not c.execute(select(meta.tables["material_uses"])).first()
        agreement = c.execute(select(meta.tables["finance_agreements"])).mappings().one()
        obligation = c.execute(select(meta.tables["finance_obligations"])).mappings().one()
        receipt = c.execute(select(meta.tables["finance_receipts"])).mappings().one()
        allocation = c.execute(select(meta.tables["finance_allocations"])).mappings().one()
        assert agreement["version"] == 4 and agreement["data"]["signed_package"] == 2
        assert obligation["version"] == 6 and obligation["due_at"] == at and obligation["amount_minor"] == 5000
        assert receipt["received_at"] == at and receipt["version"] == 3 and receipt["amount_minor"] == 1000
        assert allocation["source_key"] == "allocation-original" and allocation["amount_minor"] == 1000
        assert not c.execute(select(meta.tables["client_publications"])).first()
        assert not c.execute(select(meta.tables["operations_jobs"])).first()
        assert not compare_metadata(MigrationContext.configure(c), Base.metadata)
    Base.metadata.drop_all(engine)
    with engine.begin() as c:
        c.exec_driver_sql("DROP TABLE alembic_version")
    engine.dispose()
