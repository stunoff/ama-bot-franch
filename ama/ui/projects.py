"""Short project filters and durable project cards in both messengers."""

from sqlalchemy import select
from ama import project_flow as flow
from ama.config import settings
from ama.models import ProjectPlan, ProjectLane, ProjectBlock, ProjectChange, ProjectRemark, ProjectCampaign, Work
from ama.project_catalog import CATALOG, GROUPS
from ama.errors import AccessDenied, BusinessError
from ama.ui import access, business, schema
from ama.ui.controller import show, paginate, navigation, draft_screen
from ama.ui.engine import button as B


def client(db, actor, p):
    from ama.auth import roles_for

    return actor.binding_id == p.client_binding_id and bool(roles_for(db, actor) & {"partner", "observer"})


def visible(db, actor, p):
    if client(db, actor, p) or access.scope(db, actor, p, {"curator", "support_head", "marketing_manager"}):
        return True
    work_ids = [b.work_id for b in flow.blocks(db, p).values() if b.work_id]
    return any(
        w.executor_id == actor.person_id and access.can_work(db, actor, w, detail=True)
        for w in db.scalars(select(Work).where(Work.id.in_(work_ids)))
    )


def begin(db, actor, s, kind, target, data=None):
    draft = business.make_draft(db, actor, kind, "Проект: " + kind.split(":")[-1], data, target=target)
    return draft_screen(db, actor, s, {"draft_id": draft.id})


def dispatch(db, actor, s, action, params):
    if action == "pp_list":
        rows = [
            p
            for p in db.scalars(
                select(ProjectPlan)
                .where(ProjectPlan.bot_id == settings().bot_id)
                .order_by(ProjectPlan.created_at.desc())
            )
            if visible(db, actor, p)
        ]
        items, page = paginate(rows, params.get("page", 0))
        buttons = [B(p.title, "pp_show", project_id=p.id) for p in items]
        if access.staff(db, actor):
            buttons += [B("Новый проект…", "new", kind="plan:create"), B("Прежние проекты", "projects")]
        return show(
            db,
            actor,
            s,
            f"Проекты: {len(rows)}",
            buttons + navigation("pp_list", page, len(rows)),
            "pp_list",
            page=page,
        )
    if action == "pp_work_details":
        w = db.get(Work, params["work_id"])
        if not w or not access.can_work(db, actor, w, detail=True):
            raise AccessDenied("Работа недоступна.")
        return begin(db, actor, s, "plan:evidence", w, {"step": params.get("step")})
    if action == "pp_approval":
        c = db.get(ProjectChange, params["change_id"])
        if not c or actor.person_id not in c.data["required"]:
            raise AccessDenied("Нет назначения согласующего.")
        return begin(db, actor, s, "plan:approval", c, {})
    p = flow.get(db, params["project_id"])
    if not visible(db, actor, p):
        raise AccessDenied("Нет доступа к проекту.")
    is_client = client(db, actor, p)
    full_access = access.scope(db, actor, p, {"curator", "support_head", "marketing_manager"})
    if action == "pp_show":
        summary = flow.summary(db, p)
        text = f"{p.title}\nСценарий: {schema.SCENARIOS[p.scenario]}\nЭтап: {p.stage}\nПринято / наследовано: {summary['accepted']}; работ: {summary['works']}; ожидают: {summary['waiting']}\n"
        text += "\n".join(k + ": " + v for k, v in p.dates.items()) or "\nДаты не определены."
        actions = []
        if not is_client and full_access:
            actions += [
                B("Состав и ожидания", "pp_blocks", project_id=p.id),
                B("Календарь и контрольные даты", "pp_calendar", project_id=p.id),
                B("Замечания", "pp_remarks", project_id=p.id),
            ]
            if access.scope(db, actor, p, {"curator", "support_head"}):
                actions += [
                    B("Короткий фильтр куратора", "pp_filter", project_id=p.id, lane="curator"),
                    B("Уточнить условия…", "pp_action", project_id=p.id, kind="profile"),
                    B("Изменить дату…", "pp_action", project_id=p.id, kind="date"),
                    B("Новое замечание…", "pp_action", project_id=p.id, kind="remark"),
                ]
            if actor.person_id == p.marketing_manager_id and access.scope(db, actor, p, {"marketing_manager"}):
                actions += [
                    B("Короткий фильтр маркетолога", "pp_filter", project_id=p.id, lane="marketing"),
                    B("Регулярная кампания…", "pp_action", project_id=p.id, kind="campaign"),
                ]
        changes = list(
            db.scalars(
                select(ProjectChange).where(
                    ProjectChange.work_id.in_([b.work_id for b in flow.blocks(db, p).values() if b.work_id]),
                    ProjectChange.status == "requested",
                )
            )
        )
        for c in changes:
            if actor.person_id in c.data["required"]:
                actions.append(B("Согласование: " + c.data["decision"], "pp_approval", change_id=c.id))
                text += (
                    "\nРешение: "
                    + c.data["decision"]
                    + "\nПричина: "
                    + c.data["reason"]
                    + "\nУсловия расчётов: "
                    + c.data.get("settlement", "не указаны")
                )
        return show(db, actor, s, text, actions + [B("← Проекты", "pp_list")], "pp_show", project_id=p.id)
    if is_client or not full_access:
        raise AccessDenied("Внутренний состав проекта доступен сотрудникам.")
    if action == "pp_action":
        kind = params["kind"]
        if kind not in {"date", "profile", "remark", "campaign"}:
            raise BusinessError("Неизвестное действие проекта.")
        flow.authorize(db, actor, p, "marketing" if kind == "campaign" else "curator")
        return begin(db, actor, s, "plan:" + kind, p, {})
    if action == "pp_filter":
        lane = params["lane"]
        flow.authorize(db, actor, p, lane)
        rows = {b.data["template"]["group"] for b in flow.blocks(db, p).values() if b.lane == lane}
        return show(
            db,
            actor,
            s,
            "Выберите блок. Изменение применяется только после предпросмотра и подтверждения.",
            [B(GROUPS[g], "pp_group", project_id=p.id, lane=lane, group=g) for g in GROUPS if g in rows]
            + [B("← Проект", "pp_show", project_id=p.id)],
            "pp_filter",
            project_id=p.id,
            lane=lane,
        )
    if action == "pp_group":
        lane = params["lane"]
        flow.authorize(db, actor, p, lane)
        row = db.scalar(select(ProjectLane).where(ProjectLane.project_id == p.id, ProjectLane.name == lane))
        return begin(
            db,
            actor,
            s,
            "plan:filter",
            p,
            {
                "lane": lane,
                "lane_version": row.version,
                "group": params["group"],
                "block_code": params.get("block_code"),
            },
        )
    if action == "pp_blocks":
        rows = list(flow.blocks(db, p).values())
        items, page = paginate(rows, params.get("page", 0))
        return show(
            db,
            actor,
            s,
            "Состав: выполнить / проверить / использовать готовое / не применяется. История завершённых работ сохраняется.",
            [B(CATALOG[b.code]["title"] + " · " + b.status, "pp_block", project_id=p.id, block_id=b.id) for b in items]
            + navigation("pp_blocks", page, len(rows), project_id=p.id)
            + [B("← Проект", "pp_show", project_id=p.id)],
            "pp_blocks",
            project_id=p.id,
            page=page,
        )
    if action in {"pp_block", "pp_change"}:
        b = db.get(ProjectBlock, params["block_id"])
        if not b or b.project_id != p.id:
            raise BusinessError("Блок не найден.")
        if action == "pp_change":
            flow.authorize(db, actor, p, b.lane)
            return begin(db, actor, s, "plan:change", p, {"block_id": b.id})
        text = (
            CATALOG[b.code]["title"]
            + "\nРежим: "
            + schema.MODES[b.mode]
            + "\nСтатус: "
            + b.status
            + "\n"
            + (b.data.get("waiting_reason") or "")
        )
        if b.data.get("inheritance"):
            proof = b.data["inheritance"]
            text += "\nОснование: " + (proof.get("evidence") or proof.get("source_work_id", ""))
        buttons = []
        if not b.work_id and (
            b.lane == "curator"
            and access.scope(db, actor, p, {"curator", "support_head"})
            or b.lane == "marketing"
            and actor.person_id == p.marketing_manager_id
            and access.scope(db, actor, p, {"marketing_manager"})
        ):
            buttons.append(
                B(
                    "Изменить применимость…",
                    "pp_group",
                    project_id=p.id,
                    lane=b.lane,
                    group=CATALOG[b.code]["group"],
                    block_code=b.code,
                )
            )
        if b.work_id:
            w = db.get(Work, b.work_id)
            if w and access.can_work(db, actor, w, detail=True):
                buttons.append(B("Работа", "work", work_id=w.id))
                if b.code in {"kitchen_equipment", "systems_equipment", "community", "start_smm"}:
                    buttons.append(B("Спецификация / подтверждения…", "pp_work_details", work_id=w.id))
            if w and w.status != "done":
                buttons.append(B("Согласовать изменение…", "pp_change", project_id=p.id, block_id=b.id))
        return show(
            db,
            actor,
            s,
            text,
            buttons + [B("← Состав", "pp_blocks", project_id=p.id)],
            "pp_block",
            project_id=p.id,
            block_id=b.id,
        )
    if action == "pp_calendar":
        dates = [k + ": " + v for k, v in p.dates.items()]
        if p.dates.get("planned_technical"):
            from datetime import timedelta
            from ama.service import parse_date

            dates.append(
                "Готовность к техническому открытию (−5 календарных дней): "
                + (parse_date(p.dates["planned_technical"]) - timedelta(days=5)).isoformat()
                + " UTC"
            )
        for b in flow.blocks(db, p).values():
            item = CATALOG[b.code]
            if item.get("anchor") and b.mode != "not_applicable":
                dates.append(item["title"] + ": " + (b.data.get("waiting_reason") or b.status))
        for c in db.scalars(select(ProjectCampaign).where(ProjectCampaign.project_id == p.id)):
            dates.append(
                c.title
                + f": {c.next_at} UTC; "
                + ("активна" if c.enabled else "завершена")
                + " "
                + str(c.data.get("waiting_reason") or "")
            )
        return show(
            db,
            actor,
            s,
            "\n".join(dates) or "Даты неизвестны; просрочки не назначены.",
            [B("← Проект", "pp_show", project_id=p.id)],
            "pp_calendar",
            project_id=p.id,
        )
    if action == "pp_remarks":
        rows = list(
            db.scalars(select(ProjectRemark).where(ProjectRemark.project_id == p.id).order_by(ProjectRemark.id))
        )
        items, page = paginate(rows, params.get("page", 0))
        buttons = [
            B(r.severity + " · " + r.text[:60] + " · " + r.status, "pp_close_remark", project_id=p.id, remark_id=r.id)
            for r in items
        ]
        return show(
            db,
            actor,
            s,
            "Красные замечания блокируют открытие. Жёлтые и зелёные сохраняются при переходе этапа.",
            buttons
            + navigation("pp_remarks", page, len(rows), project_id=p.id)
            + [B("← Проект", "pp_show", project_id=p.id)],
            "pp_remarks",
            project_id=p.id,
            page=page,
        )
    if action == "pp_close_remark":
        flow.authorize(db, actor, p, "curator")
        r = db.get(ProjectRemark, params["remark_id"])
        if not r or r.project_id != p.id:
            raise BusinessError("Замечание не найдено.")
        return begin(db, actor, s, "plan:close_remark", p, {"remark_id": r.id, "remark_version": r.version})
    raise BusinessError("Неизвестный экран проекта.")
