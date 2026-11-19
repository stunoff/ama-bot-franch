"""Journal and explicit host-operated backup requests; no Docker socket in the bot."""

from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from ama.admin import check_admin
from ama.config import settings
from ama.errors import BusinessError, AccessDenied
from ama.models import OperationsJob, OperationsMutex, Audit, now
from ama.service import versioned

KINDS = {"backup", "verify_restore"}


def request(db, actor, kind, reason, origin, backup_id=None):
    if actor is not None:
        check_admin(db, actor)
    if kind not in KINDS or not reason or len(reason) > 1000:
        raise BusinessError("Выберите резервную копию / тест восстановления и укажите основание.")
    old = db.scalar(select(OperationsJob).where(OperationsJob.origin_key == origin))
    if old:
        if old.bot_id != settings().bot_id:
            raise AccessDenied("Другой бот.")
        return old
    if kind == "verify_restore":
        source = db.get(OperationsJob, backup_id)
        if not source or source.bot_id != settings().bot_id or source.kind != "backup" or source.status != "succeeded":
            raise BusinessError("Выберите успешную копию из журнала этого бота.")
    job = OperationsJob(
        bot_id=settings().bot_id,
        origin_key=origin,
        kind=kind,
        requested_by=actor.person_id if actor else None,
        data={
            "reason": reason,
            "backup_id": backup_id,
            "operator": actor is None,
            "source_binding_id": actor.binding_id if actor else None,
        },
    )
    db.add(job)
    db.flush()
    db.add(
        Audit(
            actor_id=job.requested_by,
            object_id=job.id,
            action="OPS_REQUESTED",
            data={"kind": kind, "backup_id": backup_id},
        )
    )
    return job


def mutex(db):
    key = settings().bot_id
    if not db.get(OperationsMutex, key):
        try:
            with db.begin_nested():
                db.add(OperationsMutex(bot_id=key))
                db.flush()
        except IntegrityError:
            pass
    db.scalar(select(OperationsMutex).where(OperationsMutex.bot_id == key).with_for_update())


def claim(db):
    mutex(db)
    running = db.scalar(
        select(OperationsJob)
        .where(OperationsJob.bot_id == settings().bot_id, OperationsJob.status == "running")
        .with_for_update()
    )
    if running:
        return None
    job = db.scalar(
        select(OperationsJob)
        .where(OperationsJob.bot_id == settings().bot_id, OperationsJob.status == "queued")
        .order_by(OperationsJob.created_at, OperationsJob.id)
        .with_for_update()
        .limit(1)
    )
    if not job:
        return None
    if job.requested_by:
        from ama.auth import Actor

        try:
            check_admin(db, Actor(job.requested_by, job.data.get("source_binding_id")))
        except BusinessError:
            job.status = "cancelled"
            job.version += 1
            return None
    job.status = "running"
    job.started_at = now()
    job.version += 1
    job.data = {**job.data, "claim_token": uuid4().hex}
    db.add(Audit(object_id=job.id, action="OPS_STARTED", data={"kind": job.kind}))
    return job


def finish(db, ident, token, success, details):
    job = db.scalar(select(OperationsJob).where(OperationsJob.id == ident).with_for_update())
    if not job or job.bot_id != settings().bot_id or job.status != "running" or token != job.data.get("claim_token"):
        raise BusinessError("Задание не принадлежит этому запуску.")
    allowed = {"backup_id", "manifest_sha256", "verified_tables", "verified_files", "version", "reason_code"}
    if set(details) - allowed:
        raise BusinessError("Недопустимые поля технического отчёта.")
    job.status = "succeeded" if success else "failed"
    job.finished_at = now()
    job.version += 1
    job.data = {**{k: v for k, v in job.data.items() if k != "claim_token"}, "result": details}
    db.add(Audit(object_id=job.id, action="OPS_FINISHED", data={"status": job.status, "kind": job.kind}))
    return job


def cancel(db, actor, ident, expected, reason):
    check_admin(db, actor)
    job = db.scalar(select(OperationsJob).where(OperationsJob.id == ident).with_for_update())
    if not job or job.bot_id != settings().bot_id:
        raise AccessDenied("Задание недоступно.")
    versioned(job, expected)
    if job.status != "queued" or not reason:
        raise BusinessError(
            "Отмена возможна до запуска с указанием причины; запущенную процедуру сначала сверяет оператор."
        )
    job.status = "cancelled"
    job.version += 1
    job.data = {**job.data, "cancel_reason": reason}
    db.add(Audit(actor_id=actor.person_id, object_id=job.id, action="OPS_CANCELLED", data={"reason": reason}))
