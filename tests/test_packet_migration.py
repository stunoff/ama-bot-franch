from datetime import datetime
from subprocess import run
import os
import sys
from sqlalchemy import create_engine, MetaData, select
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from ama.db import Base


def test_0005_to_0006_preserves_legacy_broadcast_and_sla(tmp_path):
    url = "sqlite:///" + str(tmp_path / "old-project.sqlite")
    engine = create_engine(url)
    env = {**os.environ, "DATABASE_URL": url}

    def upgrade(rev):
        r = run([sys.executable, "-m", "alembic", "upgrade", rev], env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    upgrade("0005")
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
    with engine.begin() as c:
        c.execute(
            meta.tables["broadcasts"].insert(),
            {
                "id": "old-broadcast",
                "author_id": "curator",
                "text": "Прежняя информация",
                "binding_ids": ["support"],
                "audience_versions": {"support": 1},
                "published_at": stamp,
                "version": 2,
            },
        )
        c.execute(
            meta.tables["sla_cases"].insert(),
            {
                "id": "old-sla",
                "work_id": "old-paid-work",
                "version": 4,
                "created_at": stamp,
                "data": {"breaches": [{"kind": "result", "reason": "Существующая история"}]},
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
        assert c.execute(select(meta.tables["sla_cases"].c.version)).scalar_one() == 4
        assert c.execute(select(meta.tables["sla_cases"].c.data)).scalar_one()["breaches"][0]["kind"] == "result"
        assert c.execute(select(meta.tables["broadcasts"].c.text)).scalar_one() == "Прежняя информация"
        assert not c.execute(select(meta.tables["packets"])).first()
        assert not c.execute(select(meta.tables["packet_obligations"])).first()
        assert not c.execute(select(meta.tables["sla_signals"])).first()
        assert not c.execute(select(meta.tables["sla_events"])).first()
        diff = compare_metadata(MigrationContext.configure(c), Base.metadata)
        assert not diff, diff
    assert meta.tables["project_blocks"] is not None
    Base.metadata.drop_all(engine)
    with engine.begin() as c:
        c.exec_driver_sql("DROP TABLE alembic_version")
    engine.dispose()
