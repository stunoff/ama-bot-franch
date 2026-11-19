"""Financial agreements, obligations, bank allocations and monthly batches."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "finance_receipts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("bot_id", sa.String(length=64), nullable=False),
        sa.Column("recipient_key", sa.String(length=100), nullable=False),
        sa.Column("bank_reference", sa.String(length=180), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("received_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False),
        sa.Column("confirmed_by", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["confirmed_by"],
            ["people.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("recipient_key", "bank_reference", name="uq_finance_bank_receipt"),
    )
    op.create_table(
        "finance_schedules",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("bot_id", sa.String(length=64), nullable=False),
        sa.Column("binding_id", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("next_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False),
        sa.Column("until_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["binding_id"],
            ["chat_bindings.id"],
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["people.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_finance_schedules_next_at"), "finance_schedules", ["next_at"], unique=False)
    op.create_table(
        "finance_agreements",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("bot_id", sa.String(length=64), nullable=False),
        sa.Column("work_id", sa.String(length=32), nullable=False),
        sa.Column("partner_id", sa.String(length=32), nullable=False),
        sa.Column("brand_id", sa.String(length=32), nullable=True),
        sa.Column("establishment_id", sa.String(length=32), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["brand_id"],
            ["entities.id"],
        ),
        sa.ForeignKeyConstraint(
            ["establishment_id"],
            ["entities.id"],
        ),
        sa.ForeignKeyConstraint(
            ["partner_id"],
            ["entities.id"],
        ),
        sa.ForeignKeyConstraint(
            ["work_id"],
            ["works.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("work_id"),
    )
    op.create_table(
        "finance_batches",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("bot_id", sa.String(length=64), nullable=False),
        sa.Column("binding_id", sa.String(length=32), nullable=False),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("work_id", sa.String(length=32), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["binding_id"],
            ["chat_bindings.id"],
        ),
        sa.ForeignKeyConstraint(
            ["work_id"],
            ["works.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bot_id", "binding_id", "period", name="uq_finance_month_batch"),
    )
    op.create_table(
        "finance_obligations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("agreement_id", sa.String(length=32), nullable=False),
        sa.Column("period", sa.String(length=100), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("due_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False),
        sa.Column("debt_work_id", sa.String(length=32), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["agreement_id"],
            ["finance_agreements.id"],
        ),
        sa.ForeignKeyConstraint(
            ["debt_work_id"],
            ["works.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agreement_id", "period", name="uq_finance_obligation_period"),
    )
    op.create_index(op.f("ix_finance_obligations_agreement_id"), "finance_obligations", ["agreement_id"], unique=False)
    op.create_table(
        "finance_allocations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("receipt_id", sa.String(length=32), nullable=False),
        sa.Column("obligation_id", sa.String(length=32), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("source_key", sa.String(length=180), nullable=False),
        sa.Column("created_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["obligation_id"],
            ["finance_obligations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["receipt_id"],
            ["finance_receipts.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_key"),
    )
    op.create_index(
        op.f("ix_finance_allocations_obligation_id"), "finance_allocations", ["obligation_id"], unique=False
    )
    op.create_index(op.f("ix_finance_allocations_receipt_id"), "finance_allocations", ["receipt_id"], unique=False)


def downgrade():
    raise RuntimeError("Digital passport history requires a coordinated backup restore.")
