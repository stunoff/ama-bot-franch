"""Upgrade an existing installation without recreating its baseline schema."""
import os
import subprocess
import sys
from pathlib import Path
from datetime import datetime
from sqlalchemy import create_engine, MetaData, inspect, select
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from ama.db import Base


def test_upgrade_0001_preserves_existing_accounts_and_personal_progress(tmp_path):
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "upgrade.sqlite"
    env = {**os.environ, "DATABASE_URL": "sqlite:///"+str(database)}
    def migrate(revision):
        result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", revision], cwd=root, env=env, text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
    migrate("0001")
    engine = create_engine(env["DATABASE_URL"])
    schema = MetaData()
    schema.reflect(engine)
    assert "version" not in {c["name"] for c in inspect(engine).get_columns("people")}
    created = datetime(2026, 11, 10, 10, 11, 12, 123456)
    with engine.begin() as con:
        con.execute(schema.tables["people"].insert(), {"id": "existing", "name": "Существующий ФП", "created_at": created})
        con.execute(schema.tables["accounts"].insert(), {"id": "account", "person_id": "existing", "platform": "telegram", "external_id": "123", "active": True})
        con.execute(schema.tables["roles"].insert(), {"id": "role", "bot_id": "ama-franchising", "person_id": "existing", "name": "curator", "active": True})
        con.execute(schema.tables["training"].insert(), {"id": "training", "person_id": "existing", "course_key": "general", "course_version": 3,
                    "cards": [{"title": "Карточка", "text": "Сохранено"}], "position": 0, "acknowledged": [0], "paused": True, "version": 4})
        con.execute(schema.tables["audit"].insert(), {"id": "audit", "created_at": created, "object_id": "training", "action": "TRAINING_NEXT", "data": {"position": 0}})
    migrate("head")
    fresh = MetaData()
    fresh.reflect(engine)
    with engine.connect() as con:
        assert con.execute(select(fresh.tables["people"].c.name, fresh.tables["people"].c.version)).one() == ("Существующий ФП", 1)
        assert con.execute(select(fresh.tables["accounts"].c.external_id, fresh.tables["accounts"].c.version)).one() == ("123", 1)
        row = con.execute(select(fresh.tables["training"])).mappings().one()
        assert row["acknowledged"] == [0] and row["version"] == 4 and row["paused"]
        assert con.execute(select(fresh.tables["audit"].c.created_at)).scalar_one() == created
        assert compare_metadata(MigrationContext.configure(con), Base.metadata) == []
    migrate("head")  # A repeated deployment must not recreate or duplicate anything.
    engine.dispose()
