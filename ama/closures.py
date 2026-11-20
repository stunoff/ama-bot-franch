"""Explicit curator closure of an informational request, distinct from FP acceptance."""

from sqlalchemy import select
from ama.auth import active_person_role
from ama.config import settings
from ama.errors import AccessDenied, BusinessError
from ama.models import Work, Binding, Record, Timer, now
from ama.service import versioned, stamp


def authorize(db, actor, w):
    b = db.get(Binding, actor.binding_id)
    source = db.get(Binding, w.binding_id) if w else None
    if (
        not w
        or not b
        or b.bot_id != settings().bot_id
        or not b.active
        or not b.verified
        or b.purpose != "support"
        or not source
        or source.bot_id != settings().bot_id
    ):
        raise AccessDenied("Закрытие выполняется куратором в сопровождении.")
    for key in ("partner_id", "brand_id", "establishment_id"):
        if getattr(b, key) and getattr(b, key) != getattr(w, key):
            raise AccessDenied("Другая область обращения.")
    if not active_person_role(db, actor.person_id, {"curator", "support_head"}, w):
        raise AccessDenied("Нужна роль куратора / руководителя сопровождения.")
    route = db.scalar(select(Record).where(Record.kind == "route", Record.key == "support", Record.published.is_(True)))
    appointed = w.data.get("curator_id") or (route.data.get("owner_id") if route else None)
    if actor.person_id != appointed and not active_person_role(db, actor.person_id, {"support_head"}, w):
        raise AccessDenied("Действие назначенного куратора.")
    if (
        not w.data.get("partner_request")
        or w.status != "review"
        or w.reviewer_id != w.partner_person_id
        or w.project_id
        or w.kind in {"paid_work", "debt", "legal"}
        or w.kind.startswith("finance_")
        or w.data.get("financial_protocol")
    ):
        raise BusinessError("Этот исход доступен только неплатному собственному обращению на подтверждении ФП.")


def execute(db, actor, ident, expected, data):
    w = db.scalar(select(Work).where(Work.id == ident).with_for_update().execution_options(populate_existing=True))
    authorize(db, actor, w)
    versioned(w, expected)
    if (
        data.get("classification") not in {"informational", "optional"}
        or not data.get("reason")
        or not data.get("basis")
        or data.get("no_financial_obligation") is not True
        or not w.data.get("result_description")
        or w.data.get("blockers")
    ):
        raise BusinessError(
            "Укажите необязательный / информационный характер, причину, основание и отсутствие оплаты; результат должен быть описан, блокеры закрыты."
        )
    previous = w.version
    w.data = {**w.data, "unconfirmed_closure": {**data, "actor_id": actor.person_id, "at": now().isoformat()}}
    w.status, w.outcome, w.owner_id = "done", "closed_without_confirmation", actor.person_id
    w.version += 1
    for t in db.scalars(select(Timer).where(Timer.work_id == w.id, Timer.status.in_(["pending", "blocked"]))):
        t.status = "cancelled"
    from ama.sla import on_transition

    on_transition(db, actor, w, "close_without_confirmation", data)
    stamp(db, actor, w, "CLOSED_WITHOUT_FP_CONFIRMATION", previous, data, "curator")
    target = db.get(Binding, w.client_binding_id)
    if (
        target
        and target.active
        and target.verified
        and target.bot_id == settings().bot_id
        and active_person_role(db, w.partner_person_id, {"partner"}, w)
    ):
        from ama.service import queue

        row = queue(
            db,
            target,
            "unconfirmed-closure:" + w.id + ":" + str(w.version),
            "Обращение закрыто куратором без подтверждения ФП.\n" + w.title + "\nПричина: " + data["reason"],
            work=w,
        )
        row.payload = {
            **row.payload,
            "unconfirmed_closure": {"work_id": w.id, "version": w.version},
            "partner_window": True,
        }
    return {"id": w.id, "outcome": w.outcome, "version": w.version}


def deliverable(db, row):
    meta = row.payload.get("unconfirmed_closure")
    if not meta:
        return True
    w = db.get(Work, meta["work_id"])
    return bool(
        w
        and w.version == meta["version"]
        and w.outcome == "closed_without_confirmation"
        and w.client_binding_id == row.binding_id
        and active_person_role(db, w.partner_person_id, {"partner"}, w)
    )
