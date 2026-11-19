"""Read-only readiness report; contains no secrets, message bodies or full database exports."""

import csv
from pathlib import Path
from hashlib import sha256
import json
from sqlalchemy import select, func, inspect, text
from ama.config import settings
from ama.models import (
    Binding,
    Record,
    Role,
    Outbox,
    Attachment,
    OperationsJob,
    Work,
    Entity,
    Account,
    EstablishmentBrand,
    now,
)
from ama.db import Base
from ama.auth import active_person_role
from ama.errors import BusinessError
from ama.ui.configuration import validate_record

VERSION = "0.11.0"


def cases():
    with (Path(__file__).resolve().parents[1] / "docs/acceptance.csv").open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def configuration_digest(db):
    payload = {}
    for model in (Binding, Record, Role, Entity, Account, EstablishmentBrand):
        payload[model.__tablename__] = [
            {column.name: getattr(row, column.name) for column in model.__table__.columns}
            for row in db.scalars(select(model).order_by(model.id))
        ]
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def report(db):
    cfg = settings()
    issues = []

    def add(code, ident="", severity="blocking"):
        issues.append({"code": code, "object_id": str(ident), "severity": severity})

    tables = set(inspect(db.connection()).get_table_names())
    missing = set(Base.metadata.tables) - tables
    for table in sorted(missing):
        add("schema_table_missing", table)
    if missing:
        return {"version": VERSION, "ready": False, "issues": issues}
    revision = (
        db.execute(text("SELECT version_num FROM alembic_version")).scalar() if "alembic_version" in tables else None
    )
    if revision != "0011":
        add("migration_required", revision or "unversioned")
    for platform in ("telegram", "max"):
        token = getattr(cfg, platform + "_token")
        secret = getattr(cfg, platform + "_webhook_secret")
        if token and len(secret) < 16:
            add("webhook_secret_missing", platform)
        if not token:
            add("platform_not_configured", platform, "warning")
    if not cfg.telegram_token and not cfg.max_token:
        add("no_platform_configured")
    if not cfg.public_url.startswith("https://"):
        add("public_https_required")
    current = list(db.scalars(select(Binding).where(Binding.bot_id == cfg.bot_id, Binding.active.is_(True))))
    for b in current:
        if not b.verified:
            add("route_not_verified", b.id)
        if b.purpose == "client":
            if not b.partner_id or not b.brand_id:
                add("client_scope_missing", b.id)
    if not any(b.purpose == "management" and b.verified for b in current):
        add("management_route_missing")
    if not any(b.purpose == "support" and b.verified for b in current):
        add("support_route_missing")
    if not db.scalar(
        select(Role.id).where(
            Role.bot_id == cfg.bot_id,
            Role.name == "admin",
            Role.active.is_(True),
            (Role.expires_at.is_(None) | (Role.expires_at > now())),
        )
    ):
        add("admin_missing")
    for r in db.scalars(select(Record).where(Record.published.is_(True))):
        try:
            validate_record(db, r.kind, r.data, published=True)
        except BusinessError:
            add("published_configuration_invalid", r.id)
        if not r.owner_id:
            add("content_owner_missing", r.id)
        if r.kind == "route":
            b = db.get(Binding, r.data.get("binding_id"))
            if not b or b.bot_id != cfg.bot_id or not b.active or not b.verified:
                add("published_route_unavailable", r.id)
            elif not r.data.get("owner_id") or not active_person_role(
                db,
                r.data.get("owner_id"),
                {
                    "curator",
                    "specialist",
                    "contract",
                    "consultant",
                    "marketing_manager",
                    "chief_marketer",
                    "lawyer",
                    "leadership",
                    "support_head",
                },
                b,
            ):
                add("published_route_owner_missing", r.id)
    pending_materials = db.scalar(
        select(func.count()).select_from(Record).where(Record.kind == "material", Record.published.is_(False))
    )
    if pending_materials:
        add("draft_materials_require_owner", str(pending_materials))
    states = {state: count for state, count in db.execute(select(Outbox.status, func.count()).group_by(Outbox.status))}
    if states.get("uncertain") or states.get("sending"):
        add("delivery_reconciliation_required", str(states.get("uncertain", 0) + states.get("sending", 0)))
    if states.get("failed") or states.get("blocked"):
        add("delivery_failures", str(states.get("failed", 0) + states.get("blocked", 0)))
    for a in db.scalars(select(Attachment).where(Attachment.status == "stored")):
        path = Path(a.storage_path or "")
        if not path.is_file():
            add("stored_file_missing", a.id)
    for w in db.scalars(select(Work).where(Work.status != "done")):
        b = db.get(Binding, w.binding_id)
        if not b or b.bot_id == cfg.bot_id and (not b.active or not b.verified):
            add("work_route_unavailable", w.id)
    from ama.storage_policy import current as storage_policy

    try:
        storage = storage_policy(db)
    except BusinessError:
        storage = None
    if not storage:
        add("approved_storage_policy_missing")
    for kind, code in (("backup", "successful_backup_missing"), ("verify_restore", "verified_restore_missing")):
        if not db.scalar(
            select(OperationsJob.id).where(
                OperationsJob.bot_id == cfg.bot_id, OperationsJob.kind == kind, OperationsJob.status == "succeeded"
            )
        ):
            add(code)
        elif storage:
            latest = db.scalar(
                select(OperationsJob)
                .where(
                    OperationsJob.bot_id == cfg.bot_id, OperationsJob.kind == kind, OperationsJob.status == "succeeded"
                )
                .order_by(OperationsJob.finished_at.desc())
                .limit(1)
            )
            from datetime import timedelta

            allowed = (
                timedelta(hours=storage.data["backup_max_age_hours"])
                if kind == "backup"
                else timedelta(days=storage.data["restore_max_age_days"])
            )
            if not latest.finished_at or latest.finished_at + allowed < now():
                add("maintenance_evidence_expired", kind)
    fingerprint = configuration_digest(db)
    evidence = list(
        db.scalars(
            select(OperationsJob).where(
                OperationsJob.bot_id == cfg.bot_id,
                OperationsJob.kind == "acceptance",
                OperationsJob.status == "succeeded",
            )
        )
    )
    latest = {}
    for e in sorted(evidence, key=lambda row: (row.created_at, row.id)):
        if e.data.get("version") == VERSION and e.data.get("configuration_digest") == fingerprint:
            latest[(e.data.get("case"), e.data.get("platform"))] = e.data.get("result")
    covered = {key for key, result in latest.items() if result in {"passed", "not_applicable"}}
    platforms = [p for p in ("telegram", "max") if getattr(cfg, p + "_token")]
    required = {(c["ID"], p) for c in cases() for p in platforms}
    if not required or required - covered:
        add("live_acceptance_incomplete", str(len(required - covered)))
    return {
        "version": VERSION,
        "schema": revision,
        "ready": not any(x["severity"] == "blocking" for x in issues),
        "issues": issues,
        "configuration_digest": fingerprint,
        "outbox": states,
        "acceptance": {"required": len(required), "covered": len(required & covered)},
    }
