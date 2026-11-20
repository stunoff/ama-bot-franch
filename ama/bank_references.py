"""Serialize all supported financial entry points, including pre-0.9 debt cards."""

from hashlib import sha256
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from ama.models import BankReferenceGuard, FinanceReceipt, Payment
from ama.config import settings
from ama.errors import BusinessError


def lock(db, reference):
    reference = (reference or "").strip()
    if not reference or len(reference) > 180:
        raise BusinessError("Нужен банковский идентификатор до 180 символов.")
    ident = sha256((settings().bot_id + "\0" + reference).encode()).hexdigest()
    if not db.get(BankReferenceGuard, ident):
        try:
            with db.begin_nested():
                db.add(BankReferenceGuard(id=ident, bot_id=settings().bot_id, data={}))
                db.flush()
        except IntegrityError:
            pass
    return db.scalar(
        select(BankReferenceGuard)
        .where(BankReferenceGuard.id == ident)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def reject_legacy(guard):
    if guard.data.get("legacy_work_ids"):
        raise BusinessError(
            "Операция уже зачтена в совместимом долговом журнале. Повторный зачёт запрещён; сверьте исходную карточку."
        )


def legacy_payment(db, work, reference):
    guard = lock(db, reference)
    reject_legacy(guard)
    if db.scalar(
        select(FinanceReceipt.id)
        .where(FinanceReceipt.bot_id == settings().bot_id, FinanceReceipt.bank_reference == reference.strip())
        .with_for_update()
    ):
        raise BusinessError("Операция уже зарегистрирована в банковском реестре обязательств.")
    if db.scalar(select(Payment.id).where(func.trim(Payment.source_key) == reference.strip()).with_for_update()):
        raise BusinessError("Операция уже зачтена в оплате франшизы.")
    guard.data = {"legacy_work_ids": [work.id]}
