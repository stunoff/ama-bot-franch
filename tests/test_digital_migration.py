"""Upgrade a populated 0.6 database; no automatic conversion of historical facts."""

from datetime import datetime
from subprocess import run
import os
import sys
from sqlalchemy import create_engine, MetaData, select
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from ama.db import Base


def test_0006_to_0007_keeps_existing_digital_and_packet_proofs(tmp_path):
    url = "sqlite:///" + str(tmp_path / "digital-upgrade.db")
    engine = create_engine(url)
    env = {**os.environ, "DATABASE_URL": url}

    def upgrade(revision):
        proc = run([sys.executable, "-m", "alembic", "upgrade", revision], env=env, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr

    upgrade("0006")
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
        assert not compare_metadata(MigrationContext.configure(c), Base.metadata)
    Base.metadata.drop_all(engine)
    with engine.begin() as c:
        c.exec_driver_sql("DROP TABLE alembic_version")
    engine.dispose()
