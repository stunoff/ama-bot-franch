import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime
from sqlalchemy import create_engine, MetaData, select, inspect
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from ama.db import Base


def test_0002_to_sales_preserves_people_deals_money_and_allows_new_purchase(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "DATABASE_URL": "sqlite:///" + str(tmp_path / "old.sqlite")}

    def upgrade(revision):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", revision], cwd=root, env=env, text=True, capture_output=True
        )
        assert result.returncode == 0, result.stderr

    upgrade("0002")
    engine = create_engine(env["DATABASE_URL"])
    metadata = MetaData()
    metadata.reflect(engine)
    stamp = datetime(2026, 11, 13, 12, 0, 0, 123456)
    with engine.begin() as con:
        con.execute(
            metadata.tables["people"].insert(), {"id": "person", "name": "Существующий клиент", "created_at": stamp}
        )
        con.execute(
            metadata.tables["leads"].insert(),
            {
                "id": "lead",
                "person_id": "person",
                "created_at": stamp,
                "source": "old-landing",
                "consent_at": stamp,
                "consent_version": "v1",
                "stage": "contract",
                "state": "active",
                "result": "success",
                "owner_id": None,
                "no_contact": False,
                "last_reaction_at": stamp,
                "version": 4,
                "data": {},
            },
        )
        con.execute(
            metadata.tables["deals"].insert(),
            {
                "id": "deal",
                "lead_id": "lead",
                "package_version": 2,
                "signed_package_version": 2,
                "required_minor": 10000,
                "currency": "RUB",
                "successful_at": stamp,
                "version": 3,
                "data": {},
            },
        )
        con.execute(
            metadata.tables["payments"].insert(),
            {
                "id": "payment",
                "deal_id": "deal",
                "source_key": "bank-old",
                "amount_minor": 10000,
                "confirmed_by": "person",
                "confirmed_at": stamp,
            },
        )
    upgrade("head")
    new = MetaData()
    new.reflect(engine)
    with engine.begin() as con:
        case = con.execute(select(new.tables["sales_cases"])).mappings().one()
        assert case["lead_id"] == "lead" and case["id"] == "lead" and case["version"] == 4
        deal = con.execute(select(new.tables["deals"])).mappings().one()
        assert deal["case_id"] == "lead" and deal["successful_at"] == stamp and deal["version"] == 3
        assert con.execute(select(new.tables["payments"].c.source_key)).scalar_one() == "bank-old"
        assert con.execute(select(new.tables["deal_interests"].c.case_id)).scalar_one() == "lead"
        con.execute(
            new.tables["sales_cases"].insert(),
            {
                "id": "expansion",
                "lead_id": "lead",
                "brand_id": None,
                "parent_id": "lead",
                "owner_id": None,
                "binding_id": None,
                "stage": "new",
                "state": "active",
                "result": None,
                "version": 1,
                "created_at": stamp,
                "data": {},
            },
        )
        con.execute(
            new.tables["deals"].insert(),
            {
                "id": "second-deal",
                "lead_id": "lead",
                "case_id": "expansion",
                "package_version": 1,
                "required_minor": 5000,
                "currency": "RUB",
                "version": 1,
                "data": {},
            },
        )
        assert compare_metadata(MigrationContext.configure(con), Base.metadata) == []
    upgrade("head")
    assert any(c["column_names"] == ["case_id"] for c in inspect(engine).get_unique_constraints("deals"))
    engine.dispose()
