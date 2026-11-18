from sqlalchemy import select
from ama.models import SLASignal, Work, Binding
from ama.config import settings
from ama.errors import AccessDenied, BusinessError
from ama.ui import access, business
from ama.ui.controller import show, paginate, navigation, draft_screen
from ama.ui.engine import button as B


def allowed(db, actor, s):
    w = db.get(Work, s.work_id)
    b = db.get(Binding, actor.binding_id)
    return bool(
        w
        and (
            s.data.get("curator_only")
            and b.purpose == "support"
            and access.scope(db, actor, w, {"curator", "support_head"})
            or not s.data.get("curator_only")
            and b.purpose == "management"
            and access.scope(db, actor, w, {"support_head"})
        )
    )


def dispatch(db, actor, screen, action, params):
    if action == "sla_client_return":
        w = db.get(Work, params["work_id"])
        from ama.auth import require

        if not w:
            raise BusinessError("Обращение не найдено.")
        require(db, actor, {"partner"}, w)
        if actor.person_id != w.partner_person_id or w.reviewer_id != actor.person_id or w.status != "review":
            raise AccessDenied("Подтверждение доступно назначенному ФП.")
        d = business.make_draft(db, actor, "client_sla:return", "Проблема не решена", {}, target=w)
        return draft_screen(db, actor, screen, {"draft_id": d.id})
    access.require_staff(db, actor)
    if action == "sla_list":
        rows = [
            s
            for s in db.scalars(
                select(SLASignal)
                .where(SLASignal.bot_id == settings().bot_id, SLASignal.status != "closed")
                .order_by(SLASignal.due_at)
            )
            if allowed(db, actor, s)
        ]
        items, page = paginate(rows, params.get("page", 0))
        return show(
            db,
            actor,
            screen,
            "Контроль SLA. Реакция руководителя не меняет исполнителя исходной работы.",
            [
                B(("Критично · " if s.critical else "") + s.kind + " · " + s.status, "sla_signal", signal_id=s.id)
                for s in items
            ]
            + navigation("sla_list", page, len(rows))
            + [B("В меню", "home")],
            "sla_list",
            page=page,
        )
    s = db.get(SLASignal, params["signal_id"])
    if not s or s.bot_id != settings().bot_id or not allowed(db, actor, s):
        raise AccessDenied("Нет доступа к управленческому сигналу.")
    if action == "sla_react":
        d = business.make_draft(db, actor, "signal:react", "Реакция на SLA", {}, target=s)
        return draft_screen(db, actor, screen, {"draft_id": d.id})
    if action == "sla_signal":
        w = db.get(Work, s.work_id)
        text = (
            s.data["reason"]
            + "\nРабота: "
            + w.title
            + "\nID: "
            + w.id
            + f"\nРеакция до {s.due_at} UTC\nСостояние: "
            + s.status
        )
        if s.data.get("reaction"):
            text += (
                "\nПоследняя реакция: " + s.data["reaction"] + "\nСледующая проверка: " + str(s.data.get("next_check"))
            )
        return show(
            db,
            actor,
            screen,
            text,
            [B("Зафиксировать реакцию…", "sla_react", signal_id=s.id), B("← Контроль SLA", "sla_list")],
            "sla_signal",
            signal_id=s.id,
        )
    raise BusinessError("Неизвестный экран контроля SLA.")
