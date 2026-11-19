"""Explicit client publications and operations journal, preserving all legacy rows."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "operations_mutex", sa.Column("bot_id", sa.String(length=64), nullable=False), sa.PrimaryKeyConstraint("bot_id")
    )
    op.create_table(
        "operations_jobs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("bot_id", sa.String(length=64), nullable=False),
        sa.Column("origin_key", sa.String(length=180), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("requested_by", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False),
        sa.Column("started_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=True),
        sa.Column("finished_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["people.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("origin_key"),
    )
    op.create_index(op.f("ix_operations_jobs_bot_id"), "operations_jobs", ["bot_id"], unique=False)
    op.create_index(op.f("ix_operations_jobs_status"), "operations_jobs", ["status"], unique=False)
    op.create_table(
        "client_publications",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("bot_id", sa.String(length=64), nullable=False),
        sa.Column("origin_key", sa.String(length=180), nullable=False),
        sa.Column("work_id", sa.String(length=32), nullable=False),
        sa.Column("author_id", sa.String(length=32), nullable=False),
        sa.Column("source_binding_id", sa.String(length=32), nullable=False),
        sa.Column("target_binding_id", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["people.id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_binding_id"],
            ["chat_bindings.id"],
        ),
        sa.ForeignKeyConstraint(
            ["target_binding_id"],
            ["chat_bindings.id"],
        ),
        sa.ForeignKeyConstraint(
            ["work_id"],
            ["works.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("origin_key"),
    )
    op.create_index(op.f("ix_client_publications_bot_id"), "client_publications", ["bot_id"], unique=False)
    op.create_index(op.f("ix_client_publications_work_id"), "client_publications", ["work_id"], unique=False)


def downgrade():
    raise RuntimeError("Publication and operations history requires a coordinated backup restore.")
