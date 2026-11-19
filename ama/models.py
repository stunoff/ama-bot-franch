"""Серверные идентичности. Внешние ID всегда строки; даты — UTC без tzinfo в БД."""
import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Text, Integer, Boolean, DateTime, JSON, ForeignKey, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.mysql import DATETIME

from ama.db import Base

UTC_DATETIME = DateTime().with_variant(DATETIME(fsp=6), "mysql")


def uid():
    return uuid.uuid4().hex


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Identity(Base):
    __tablename__ = "people"
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now)


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("platform", "external_id", name="uq_account"),)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    platform: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Entity(Base):
    """Партнёр, бренд, физическое предприятие; общая площадка не дублируется по брендам."""
    __tablename__ = "entities"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    name: Mapped[str] = mapped_column(String(200))
    partner_id: Mapped[str | None] = mapped_column(ForeignKey("entities.id"), nullable=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)


class EstablishmentBrand(Base):
    __tablename__ = "establishment_brands"
    __table_args__ = (UniqueConstraint("establishment_id", "brand_id", name="uq_establishment_brand"),)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    establishment_id: Mapped[str] = mapped_column(ForeignKey("entities.id"))
    brand_id: Mapped[str] = mapped_column(ForeignKey("entities.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Role(Base):
    __tablename__ = "roles"
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    bot_id: Mapped[str] = mapped_column(String(64), default="ama-franchising")
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), index=True)
    name: Mapped[str] = mapped_column(String(40))
    partner_id: Mapped[str | None] = mapped_column(ForeignKey("entities.id"), nullable=True)
    brand_id: Mapped[str | None] = mapped_column(ForeignKey("entities.id"), nullable=True)
    establishment_id: Mapped[str | None] = mapped_column(ForeignKey("entities.id"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME, nullable=True)


class Binding(Base):
    __tablename__ = "chat_bindings"
    __table_args__ = (UniqueConstraint("bot_id", "platform", "chat_id", "topic_key", name="uq_chat_route"),
                     UniqueConstraint("active_client_key", name="uq_active_client"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    bot_id: Mapped[str] = mapped_column(String(64), default="ama-franchising")
    platform: Mapped[str] = mapped_column(String(16))
    chat_id: Mapped[str] = mapped_column(String(64))
    topic_key: Mapped[str] = mapped_column(String(64), default="")
    purpose: Mapped[str] = mapped_column(String(64))
    partner_id: Mapped[str | None] = mapped_column(ForeignKey("entities.id"), nullable=True)
    brand_id: Mapped[str | None] = mapped_column(ForeignKey("entities.id"), nullable=True)
    establishment_id: Mapped[str | None] = mapped_column(ForeignKey("entities.id"), nullable=True)
    active_client_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)


class Record(Base):
    """Версионированные справочники: calendar/material/template/course/route/commercial."""
    __tablename__ = "configuration"
    __table_args__ = (UniqueConstraint("kind", "key", name="uq_config_key"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    kind: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(100))
    data: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)


class Work(Base):
    __tablename__ = "works"
    __table_args__ = (UniqueConstraint("origin_key", name="uq_work_origin"), Index("ix_work_queue", "owner_id", "status"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    origin_key: Mapped[str | None] = mapped_column(String(180), nullable=True)
    kind: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(250))
    partner_id: Mapped[str] = mapped_column(ForeignKey("entities.id"))
    brand_id: Mapped[str | None] = mapped_column(ForeignKey("entities.id"), nullable=True)
    establishment_id: Mapped[str | None] = mapped_column(ForeignKey("entities.id"), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("works.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="new")
    outcome: Mapped[str | None] = mapped_column(String(40), nullable=True)
    priority: Mapped[str] = mapped_column(String(20), default="normal")
    owner_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    reviewer_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    executor_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    partner_person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    binding_id: Mapped[str] = mapped_column(ForeignKey("chat_bindings.id"))
    client_binding_id: Mapped[str | None] = mapped_column(ForeignKey("chat_bindings.id"), nullable=True)
    review_binding_id: Mapped[str | None] = mapped_column(ForeignKey("chat_bindings.id"), nullable=True)
    pending_owner_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    pending_binding_id: Mapped[str | None] = mapped_column(ForeignKey("chat_bindings.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now)
    due_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    original_due_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    version: Mapped[int] = mapped_column(Integer, default=1)
    returns: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class Audit(Base):
    __tablename__ = "audit"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now)
    actor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    role: Mapped[str | None] = mapped_column(String(40), nullable=True)
    object_id: Mapped[str] = mapped_column(String(100), index=True)
    action: Mapped[str] = mapped_column(String(64))
    old_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class Receipt(Base):
    __tablename__ = "command_receipts"
    key: Mapped[str] = mapped_column(String(180), primary_key=True)
    response: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now)


class Inbox(Base):
    __tablename__ = "inbox"
    key: Mapped[str] = mapped_column(String(180), primary_key=True)
    platform: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    retry_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Outbox(Base):
    __tablename__ = "outbox"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    dedupe_key: Mapped[str] = mapped_column(String(180), unique=True)
    binding_id: Mapped[str | None] = mapped_column(ForeignKey("chat_bindings.id"), nullable=True)
    binding_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    platform: Mapped[str] = mapped_column(String(16))
    chat_id: Mapped[str] = mapped_column(String(64))
    topic_key: Mapped[str] = mapped_column(String(64), default="")
    payload: Mapped[dict] = mapped_column(JSON)
    work_id: Mapped[str | None] = mapped_column(ForeignKey("works.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    retry_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now)
    external_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now)


class Timer(Base):
    __tablename__ = "timers"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    key: Mapped[str] = mapped_column(String(180), unique=True)
    work_id: Mapped[str] = mapped_column(ForeignKey("works.id"))
    due_at: Mapped[datetime] = mapped_column(UTC_DATETIME, index=True)
    original_due_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class Attachment(Base):
    __tablename__ = "attachments"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    source_key: Mapped[str] = mapped_column(String(180), unique=True)
    work_id: Mapped[str] = mapped_column(ForeignKey("works.id"))
    author_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    source_binding_id: Mapped[str | None] = mapped_column(ForeignKey("chat_bindings.id"), nullable=True)
    visibility: Mapped[str] = mapped_column(String(16), default="internal")
    platform: Mapped[str] = mapped_column(String(16))
    file_id: Mapped[str] = mapped_column(Text)
    album_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    filename: Mapped[str] = mapped_column(String(250))
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now)


class Training(Base):
    __tablename__ = "training"
    __table_args__ = (UniqueConstraint("person_id", "course_key", "course_version", name="uq_person_course"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    course_key: Mapped[str] = mapped_column(String(100))
    course_version: Mapped[int] = mapped_column(Integer)
    cards: Mapped[list] = mapped_column(JSON)
    position: Mapped[int] = mapped_column(Integer, default=0)
    acknowledged: Mapped[list] = mapped_column(JSON, default=list)
    completed_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME, nullable=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)


class Lead(Base):
    __tablename__ = "leads"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now)
    source: Mapped[str] = mapped_column(String(100))
    consent_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    consent_version: Mapped[str] = mapped_column(String(64))
    stage: Mapped[str] = mapped_column(String(40), default="new")
    state: Mapped[str] = mapped_column(String(40), default="active")
    result: Mapped[str | None] = mapped_column(String(40), nullable=True)
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    no_contact: Mapped[bool] = mapped_column(Boolean, default=False)
    last_reaction_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now)
    version: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class Interest(Base):
    __tablename__ = "interests"
    __table_args__ = (UniqueConstraint("lead_id", "brand_id", name="uq_interest"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"))
    brand_id: Mapped[str] = mapped_column(ForeignKey("entities.id"))


class Deal(Base):
    __tablename__ = "deals"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"), index=True)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("sales_cases.id"), nullable=True, unique=True)
    package_version: Mapped[int] = mapped_column(Integer, default=1)
    signed_package_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    required_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="RUB")
    successful_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME, nullable=True)
    curator_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    handoff_accepted_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    deal_id: Mapped[str] = mapped_column(ForeignKey("deals.id"))
    source_key: Mapped[str] = mapped_column(String(180), unique=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    confirmed_by: Mapped[str] = mapped_column(ForeignKey("people.id"))
    confirmed_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now)


class Broadcast(Base):
    __tablename__ = "broadcasts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    author_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    text: Mapped[str] = mapped_column(Text)
    binding_ids: Mapped[list] = mapped_column(JSON)
    audience_versions: Mapped[dict] = mapped_column(JSON)
    published_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class Dialog(Base):
    __tablename__ = "dialogs"
    __table_args__ = (UniqueConstraint("person_id", "binding_id", name="uq_dialog_context"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    binding_id: Mapped[str] = mapped_column(ForeignKey("chat_bindings.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    stage: Mapped[str] = mapped_column(String(24), default="enterprise")
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class LeadTouch(Base):
    __tablename__ = "lead_touches"
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    __table_args__ = (UniqueConstraint("lead_id", "epoch", "ordinal", name="uq_lead_silence_touch"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"))
    epoch: Mapped[str] = mapped_column(String(32))
    ordinal: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(32))
    due_at: Mapped[datetime] = mapped_column(UTC_DATETIME, index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")


class MediaGroup(Base):
    __tablename__ = "media_groups"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    work_id: Mapped[str] = mapped_column(ForeignKey("works.id"))
    author_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    binding_id: Mapped[str] = mapped_column(ForeignKey("chat_bindings.id"))


class UIScreen(Base):
    __tablename__ = "ui_screens"
    __table_args__ = (UniqueConstraint("bot_id", "person_id", "binding_id", name="uq_ui_screen"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    bot_id: Mapped[str] = mapped_column(String(64))
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    binding_id: Mapped[str] = mapped_column(ForeignKey("chat_bindings.id"))
    binding_version: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now)


class UIDraft(Base):
    __tablename__ = "ui_drafts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    bot_id: Mapped[str] = mapped_column(String(64))
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), index=True)
    binding_id: Mapped[str] = mapped_column(ForeignKey("chat_bindings.id"))
    kind: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(250))
    target_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    before: Mapped[dict] = mapped_column(JSON, default=dict)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="draft")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now)
    updated_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now)


class ConfigurationRevision(Base):
    __tablename__ = "configuration_revisions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    object_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    old_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    before: Mapped[dict] = mapped_column(JSON, default=dict)
    after: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now)


class UIExport(Base):
    __tablename__ = "ui_exports"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    bot_id: Mapped[str] = mapped_column(String(64))
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    binding_id: Mapped[str] = mapped_column(ForeignKey("chat_bindings.id"))
    binding_version: Mapped[int] = mapped_column(Integer)
    grants_hash: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now)
    expires_at: Mapped[datetime] = mapped_column(UTC_DATETIME)


class SalesCase(Base):
    """One confirmed buying interest; a person can have several without losing prior success."""
    __tablename__ = "sales_cases"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"), index=True)
    brand_id: Mapped[str | None] = mapped_column(ForeignKey("entities.id"), nullable=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("sales_cases.id"), nullable=True)
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    binding_id: Mapped[str | None] = mapped_column(ForeignKey("chat_bindings.id"), nullable=True)
    stage: Mapped[str] = mapped_column(String(40), default="new")
    state: Mapped[str] = mapped_column(String(40), default="active")
    result: Mapped[str | None] = mapped_column(String(40), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class ProspectSession(Base):
    __tablename__ = "prospect_sessions"
    __table_args__ = (UniqueConstraint("bot_id", "person_id", "platform", "chat_id", name="uq_prospect_session"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    bot_id: Mapped[str] = mapped_column(String(64))
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    platform: Mapped[str] = mapped_column(String(16))
    chat_id: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("sales_cases.id"), nullable=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now)


class LandingTicket(Base):
    __tablename__ = "landing_tickets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    source_key: Mapped[str] = mapped_column(String(100))
    platform: Mapped[str] = mapped_column(String(16))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now)
    expires_at: Mapped[datetime] = mapped_column(UTC_DATETIME)


class SalesEvent(Base):
    __tablename__ = "sales_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    event_key: Mapped[str] = mapped_column(String(180), unique=True)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"), index=True)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("sales_cases.id"), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    kind: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now)


class SalesControl(Base):
    __tablename__ = "sales_controls"
    retry_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now, index=True)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(ForeignKey("sales_cases.id"))
    event_key: Mapped[str] = mapped_column(String(180), unique=True)
    kind: Mapped[str] = mapped_column(String(40))
    due_at: Mapped[datetime] = mapped_column(UTC_DATETIME, index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class SalesExpert(Base):
    __tablename__ = "sales_experts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(ForeignKey("sales_cases.id"), index=True)
    requester_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    owner_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    binding_id: Mapped[str] = mapped_column(ForeignKey("chat_bindings.id"))
    category: Mapped[str] = mapped_column(String(40))
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(20), default="new")
    due_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    version: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class DealInterest(Base):
    __tablename__ = "deal_interests"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    deal_id: Mapped[str] = mapped_column(ForeignKey("deals.id"), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("sales_cases.id"), unique=True)


class ProjectPlan(Base):
    __tablename__ = "project_plans"
    next_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now, index=True)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    bot_id: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(250))
    partner_id: Mapped[str] = mapped_column(ForeignKey("entities.id"))
    establishment_id: Mapped[str] = mapped_column(ForeignKey("entities.id"))
    brand_id: Mapped[str | None] = mapped_column(ForeignKey("entities.id"), nullable=True)
    curator_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    marketing_manager_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    partner_person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    client_binding_id: Mapped[str | None] = mapped_column(ForeignKey("chat_bindings.id"), nullable=True)
    source_deal_id: Mapped[str | None] = mapped_column(ForeignKey("deals.id"), nullable=True)
    scenario: Mapped[str] = mapped_column(String(40))
    stage: Mapped[str] = mapped_column(String(40), default="preparation")
    version: Mapped[int] = mapped_column(Integer, default=1)
    profile: Mapped[dict] = mapped_column(JSON, default=dict)
    dates: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now)


class ProjectLane(Base):
    __tablename__ = "project_lanes"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_project_lane"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("project_plans.id"))
    name: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class ProjectBlock(Base):
    __tablename__ = "project_blocks"
    __table_args__ = (UniqueConstraint("project_id", "code", name="uq_project_block"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("project_plans.id"), index=True)
    code: Mapped[str] = mapped_column(String(100))
    lane: Mapped[str] = mapped_column(String(32))
    mode: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(32), default="waiting")
    work_id: Mapped[str | None] = mapped_column(ForeignKey("works.id"), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class FacilityResult(Base):
    __tablename__ = "facility_results"
    __table_args__ = (UniqueConstraint("active_key", name="uq_facility_result"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    establishment_id: Mapped[str] = mapped_column(ForeignKey("entities.id"))
    code: Mapped[str] = mapped_column(String(100))
    active_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    work_id: Mapped[str | None] = mapped_column(ForeignKey("works.id"), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class ProjectRevision(Base):
    __tablename__ = "project_revisions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("project_plans.id"), index=True)
    actor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lane: Mapped[str] = mapped_column(String(32))
    old_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_version: Mapped[int] = mapped_column(Integer)
    before: Mapped[dict] = mapped_column(JSON, default=dict)
    after: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now)


class ProjectChange(Base):
    __tablename__ = "project_changes"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("project_plans.id"))
    block_id: Mapped[str] = mapped_column(ForeignKey("project_blocks.id"))
    work_id: Mapped[str] = mapped_column(ForeignKey("works.id"))
    work_version: Mapped[int] = mapped_column(Integer)
    requested_by: Mapped[str] = mapped_column(ForeignKey("people.id"))
    status: Mapped[str] = mapped_column(String(24), default="requested")
    version: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[dict] = mapped_column(JSON)
    approvals: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now)


class ProjectRemark(Base):
    __tablename__ = "project_remarks"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("project_plans.id"), index=True)
    work_id: Mapped[str | None] = mapped_column(ForeignKey("works.id"), nullable=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    severity: Mapped[str] = mapped_column(String(12))
    text: Mapped[str] = mapped_column(Text)
    due_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open")
    version: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class ProjectCampaign(Base):
    __tablename__ = "project_campaigns"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("project_plans.id"), index=True)
    title: Mapped[str] = mapped_column(String(250))
    owner_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    next_at: Mapped[datetime] = mapped_column(UTC_DATETIME, index=True)
    until_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[dict] = mapped_column(JSON)


class SLACase(Base):
    __tablename__ = "sla_cases"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    work_id: Mapped[str] = mapped_column(ForeignKey("works.id"), unique=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class SLASignal(Base):
    __tablename__ = "sla_signals"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    bot_id: Mapped[str] = mapped_column(String(64))
    work_id: Mapped[str] = mapped_column(ForeignKey("works.id"), index=True)
    event_key: Mapped[str] = mapped_column(String(180), unique=True)
    kind: Mapped[str] = mapped_column(String(40))
    critical: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(24), default="open")
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    due_at: Mapped[datetime] = mapped_column(UTC_DATETIME, index=True)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now)
    version: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class SLAEvent(Base):
    __tablename__ = "sla_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    work_id: Mapped[str] = mapped_column(ForeignKey("works.id"), index=True)
    signal_id: Mapped[str | None] = mapped_column(ForeignKey("sla_signals.id"), nullable=True)
    event_key: Mapped[str] = mapped_column(String(180), unique=True)
    kind: Mapped[str] = mapped_column(String(40))
    due_at: Mapped[datetime] = mapped_column(UTC_DATETIME, index=True)
    retry_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now, index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class Packet(Base):
    __tablename__ = "packets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    bot_id: Mapped[str] = mapped_column(String(64))
    author_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    source_binding_id: Mapped[str] = mapped_column(ForeignKey("chat_bindings.id"))
    topic_binding_id: Mapped[str | None] = mapped_column(ForeignKey("chat_bindings.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(250))
    current_revision: Mapped[int] = mapped_column(Integer, default=1)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now)


class PacketRevision(Base):
    __tablename__ = "packet_revisions"
    __table_args__ = (UniqueConstraint("packet_id", "number", name="uq_packet_revision"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    packet_id: Mapped[str] = mapped_column(ForeignKey("packets.id"))
    number: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[dict] = mapped_column(JSON)
    audience: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now)


class PacketPublication(Base):
    __tablename__ = "packet_publications"
    __table_args__ = (UniqueConstraint("revision_id", "binding_id", name="uq_packet_publication"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    revision_id: Mapped[str] = mapped_column(ForeignKey("packet_revisions.id"), index=True)
    binding_id: Mapped[str] = mapped_column(ForeignKey("chat_bindings.id"))
    binding_version: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1)
    delivered_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME, nullable=True)
    data: Mapped[dict] = mapped_column(JSON)


class PacketObligation(Base):
    __tablename__ = "packet_obligations"
    __table_args__ = (UniqueConstraint("revision_id", "scope_key", name="uq_packet_obligation"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    revision_id: Mapped[str] = mapped_column(ForeignKey("packet_revisions.id"), index=True)
    scope_key: Mapped[str] = mapped_column(String(180))
    partner_id: Mapped[str] = mapped_column(ForeignKey("entities.id"))
    establishment_id: Mapped[str | None] = mapped_column(ForeignKey("entities.id"), nullable=True)
    brand_id: Mapped[str | None] = mapped_column(ForeignKey("entities.id"), nullable=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    curator_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    binding_id: Mapped[str] = mapped_column(ForeignKey("chat_bindings.id"))
    work_id: Mapped[str | None] = mapped_column(ForeignKey("works.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    due_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    next_at: Mapped[datetime] = mapped_column(UTC_DATETIME, default=now, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class PacketQuestion(Base):
    __tablename__ = "packet_questions"
    __table_args__ = (UniqueConstraint("revision_id", "partner_id", "person_id", "scope_key", name="uq_packet_question"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    revision_id: Mapped[str] = mapped_column(ForeignKey("packet_revisions.id"))
    partner_id: Mapped[str] = mapped_column(ForeignKey("entities.id"))
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    scope_key: Mapped[str] = mapped_column(String(180))
    work_id: Mapped[str] = mapped_column(ForeignKey("works.id"))


class PacketAsset(Base):
    __tablename__ = "packet_assets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    author_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    binding_id: Mapped[str] = mapped_column(ForeignKey("chat_bindings.id"))
    draft_id: Mapped[str] = mapped_column(ForeignKey("ui_drafts.id"))
    platform: Mapped[str] = mapped_column(String(16))
    file_id: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(String(250))
    source_key: Mapped[str] = mapped_column(String(180), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)


class PacketSchedule(Base):
    __tablename__ = "packet_schedules"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    bot_id: Mapped[str] = mapped_column(String(64))
    author_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    binding_id: Mapped[str] = mapped_column(ForeignKey("chat_bindings.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_at: Mapped[datetime] = mapped_column(UTC_DATETIME, index=True)
    until_at: Mapped[datetime] = mapped_column(UTC_DATETIME)
    data: Mapped[dict] = mapped_column(JSON)
