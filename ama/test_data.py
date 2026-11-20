"""One explicit test marker inherited by business reports; never rewrites signed facts."""

from sqlalchemy import select
from ama.models import Entity, Lead, Work, Deal, SalesCase, Audit
from ama.admin import check_admin
from ama.errors import BusinessError


def is_test(db, obj, seen=None):
    if not obj:
        return False
    seen = set() if seen is None else seen
    key = (type(obj).__name__, obj.id)
    if key in seen:
        return False
    seen.add(key)
    data = getattr(obj, "data", {}) or {}
    if data.get("is_test") is True or data.get("test") is True:
        return True
    if isinstance(obj, Entity):
        return is_test(db, db.get(Entity, obj.partner_id), seen) if obj.partner_id else False
    if isinstance(obj, SalesCase) or isinstance(obj, Deal):
        return is_test(db, db.get(Lead, obj.lead_id), seen)
    for name in ("partner_id", "establishment_id"):
        ident = getattr(obj, name, None)
        if ident and is_test(db, db.get(Entity, ident), seen):
            return True
    return False


def mark(db, actor, kind, ident, reason):
    check_admin(db, actor)
    models = {"partner": Entity, "establishment": Entity, "lead": Lead, "work": Work}
    if kind not in models or not reason:
        raise BusinessError("Выберите тестовую запись и основание.")
    obj = db.get(models[kind], ident)
    if not obj or isinstance(obj, Entity) and obj.kind != kind:
        raise BusinessError("Запись не найдена.")
    if isinstance(obj, Lead) and db.scalar(select(Deal.id).where(Deal.lead_id == obj.id)):
        raise BusinessError("После договорного старта маркировка лида требует отдельной сверки, история не скрывается.")
    if isinstance(obj, Work) and (
        obj.status == "done" or obj.data.get("paid_started_at") or obj.data.get("finance_agreement")
    ):
        raise BusinessError("Нельзя задним числом скрыть подтверждённую / финансовую работу как тестовую.")
    if isinstance(obj, Entity):
        if db.scalar(select(Work.id).where((Work.partner_id == obj.id) | (Work.establishment_id == obj.id))):
            raise BusinessError("Тестовый контур отмечается до создания бизнес-работ.")
    obj.data = {**obj.data, "is_test": True}
    obj.version += 1
    db.add(
        Audit(
            actor_id=actor.person_id,
            object_id=obj.id,
            action="TEST_SCOPE_MARKED",
            data={"kind": kind, "reason": reason},
        )
    )
    return {"id": obj.id, "version": obj.version, "test_record": True}
