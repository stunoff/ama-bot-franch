"""Persistent screens, drafts, configuration history and scoped CSV exports."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None
D = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade():
    for table in ("people", "accounts", "roles", "establishment_brands"):
        op.add_column(table, sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.create_table("ui_screens",
        sa.Column("id", sa.String(32), primary_key=True), sa.Column("bot_id", sa.String(64), nullable=False),
        sa.Column("person_id", sa.String(32), sa.ForeignKey("people.id"), nullable=False),
        sa.Column("binding_id", sa.String(32), sa.ForeignKey("chat_bindings.id"), nullable=False),
        sa.Column("binding_version", sa.Integer(), nullable=False), sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False), sa.Column("updated_at", D, nullable=False),
        sa.UniqueConstraint("bot_id", "person_id", "binding_id", name="uq_ui_screen"))
    op.create_table("ui_drafts",
        sa.Column("id", sa.String(32), primary_key=True), sa.Column("bot_id", sa.String(64), nullable=False),
        sa.Column("person_id", sa.String(32), sa.ForeignKey("people.id"), nullable=False),
        sa.Column("binding_id", sa.String(32), sa.ForeignKey("chat_bindings.id"), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False), sa.Column("title", sa.String(250), nullable=False),
        sa.Column("target_id", sa.String(32)), sa.Column("target_version", sa.Integer()),
        sa.Column("before", sa.JSON(), nullable=False), sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False), sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", D, nullable=False), sa.Column("updated_at", D, nullable=False))
    op.create_index("ix_ui_drafts_person_id", "ui_drafts", ["person_id"])
    op.create_table("configuration_revisions",
        sa.Column("id", sa.String(32), primary_key=True), sa.Column("object_id", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False), sa.Column("actor_id", sa.String(32)),
        sa.Column("old_version", sa.Integer()), sa.Column("new_version", sa.Integer()),
        sa.Column("before", sa.JSON(), nullable=False), sa.Column("after", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False), sa.Column("created_at", D, nullable=False))
    op.create_index("ix_configuration_revisions_object_id", "configuration_revisions", ["object_id"])
    op.create_table("ui_exports",
        sa.Column("id", sa.String(32), primary_key=True), sa.Column("bot_id", sa.String(64), nullable=False),
        sa.Column("person_id", sa.String(32), sa.ForeignKey("people.id"), nullable=False),
        sa.Column("binding_id", sa.String(32), sa.ForeignKey("chat_bindings.id"), nullable=False),
        sa.Column("binding_version", sa.Integer(), nullable=False), sa.Column("grants_hash", sa.String(64), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False), sa.Column("status", sa.String(24), nullable=False),
        sa.Column("storage_path", sa.Text()), sa.Column("created_at", D, nullable=False), sa.Column("expires_at", D, nullable=False))


def downgrade():
    for table in ("ui_exports", "configuration_revisions", "ui_drafts", "ui_screens"):
        op.drop_table(table)
    for table in ("people", "accounts", "roles", "establishment_brands"):
        op.drop_column(table, "version")
