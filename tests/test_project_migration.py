from datetime import datetime
from subprocess import run
import os
import sys
from sqlalchemy import create_engine, MetaData, select
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from ama.db import Base


def test_0003_to_0004_preserves_existing_paid_work(tmp_path):
    url = "sqlite:///" + str(tmp_path / "old-project.sqlite")
    engine = create_engine(url)
    env = {**os.environ, "DATABASE_URL": url}

    def upgrade(rev):
        r = run([sys.executable, "-m", "alembic", "upgrade", rev], env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    upgrade("0003")
    meta = MetaData()
    meta.reflect(engine)
    stamp = datetime(2026, 11, 17, 12, 34, 56, 123456)
    with engine.begin() as c:
        c.execute(meta.tables["people"].insert(), [{"id": "curator", "name": "Куратор", "created_at": stamp}])
        c.execute(
            meta.tables["entities"].insert(),
            {"id": "partner", "kind": "partner", "name": "Партнёр", "data": {}, "version": 1},
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
        c.execute(
            meta.tables["works"].insert(),
            {
                "id": "old-paid-work",
                "kind": "paid_work",
                "title": "Оплаченная работа",
                "status": "in_progress",
                "priority": "normal",
                "partner_id": "partner",
                "owner_id": "curator",
                "executor_id": "curator",
                "reviewer_id": "curator",
                "binding_id": "support",
                "due_at": stamp,
                "original_due_at": stamp,
                "version": 7,
                "returns": 2,
                "data": {"paid_started_at": "2026-11-17", "ledger": [{"amount_minor": 123456}]},
                "created_at": stamp,
            },
        )
    upgrade("head")
    upgrade("head")
    meta = MetaData()
    meta.reflect(engine)
    with engine.connect() as c:
        w = c.execute(select(meta.tables["works"])).mappings().one()
        assert w["version"] == 7 and w["returns"] == 2 and w["review_binding_id"] is None
        assert w["data"]["ledger"] == [{"amount_minor": 123456}] and w["original_due_at"] == stamp
        assert not c.execute(select(meta.tables["project_plans"])).first()
        diff = compare_metadata(MigrationContext.configure(c), Base.metadata)
        assert not diff, diff
    assert meta.tables["project_blocks"] is not None
    Base.metadata.drop_all(engine)
    with engine.begin() as c:
        c.exec_driver_sql("DROP TABLE alembic_version")
    engine.dispose()
