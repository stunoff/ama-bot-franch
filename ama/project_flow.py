"""Persistent project plans. All callers own the surrounding transaction."""

from copy import deepcopy
from datetime import timedelta
from calendar import monthrange
from types import SimpleNamespace
from sqlalchemy import select
from ama.auth import active_person_role
from ama.config import settings
from ama.errors import AccessDenied, BusinessError, Conflict
from ama.models import (
    ProjectPlan,
    ProjectLane,
    ProjectBlock,
    ProjectRevision,
    ProjectChange,
    ProjectRemark,
    ProjectCampaign,
    FacilityResult,
    Entity,
    Work,
    Binding,
    Record,
    Training,
    Timer,
    now,
)
from ama.project_catalog import CATALOG, GROUPS, VERSION, default_mode
from ama.service import validate_scope, versioned, parse_date, queue, make_timer, stamp, STAFF
from ama.calendar import Calendar
from ama.ui import access

MODES = {"perform", "verify", "inherit", "not_applicable"}
SCENARIOS = {"new", "additional_brand", "existing", "technical", "reconstruction"}
DATE_KEYS = {
    "planned_internal",
    "planned_technical",
    "planned_official",
    "planned_event",
    "actual_internal",
    "actual_technical",
    "actual_official",
    "actual_event",
}


def get(db, project_id, lock=False):
    q = select(ProjectPlan).where(ProjectPlan.id == project_id, ProjectPlan.bot_id == settings().bot_id)
    p = db.scalar(q.with_for_update() if lock else q)
    if not p:
        raise BusinessError("Проект не найден.")
    return p


def authorize(db, actor, p, lane=None):
    wanted = (
        {"marketing_manager"}
        if lane == "marketing"
        else {"curator", "support_head"}
        if lane == "curator"
        else {"curator", "support_head", "marketing_manager"}
    )
    if not access.scope(db, actor, p, wanted):
        raise AccessDenied("Нет назначения в области проекта и профильном чате.")
    if lane == "marketing" and actor.person_id != p.marketing_manager_id:
        raise AccessDenied("Маркетинговый состав ведёт назначенный менеджер.")
    if lane == "curator" and actor.person_id != p.curator_id and not access.scope(db, actor, p, {"support_head"}):
        raise AccessDenied("Состав ведёт закреплённый куратор.")


def blocks(db, p):
    return {
        b.code: b
        for b in db.scalars(select(ProjectBlock).where(ProjectBlock.project_id == p.id).order_by(ProjectBlock.code))
    }


def revision(db, actor, p, lane, old, before, after, reason):
    db.add(
        ProjectRevision(
            project_id=p.id,
            actor_id=actor.person_id if actor else None,
            lane=lane,
            old_version=old,
            new_version=old + 1,
            before=deepcopy(before),
            after=deepcopy(after),
            reason=reason,
        )
    )


def create(db, actor, data, key):
    old = db.get(ProjectPlan, key)
    if old:
        authorize(db, actor, old, "curator")
        return summary(db, old)
    if not data.get("brand_id"):
        raise BusinessError("Выберите бренд проекта.")
    scenario = data.get("scenario")
    if scenario not in SCENARIOS:
        raise BusinessError("Выберите сценарий.")
    validate_scope(db, data["partner_id"], data.get("brand_id"), data["establishment_id"])
    p = ProjectPlan(
        id=key,
        bot_id=settings().bot_id,
        title=data["title"],
        partner_id=data["partner_id"],
        establishment_id=data["establishment_id"],
        brand_id=data.get("brand_id"),
        curator_id=actor.person_id,
        marketing_manager_id=data.get("marketing_manager_id"),
        partner_person_id=data.get("partner_person_id"),
        client_binding_id=data.get("client_binding_id"),
        scenario=scenario,
        version=1,
        dates={},
        profile={"matrix_version": VERSION, "flags": {}, "next_scan": now().isoformat()},
    )
    authorize(db, actor, p, "curator")
    for person, names in [(p.marketing_manager_id, {"marketing_manager"}), (p.partner_person_id, {"partner"})]:
        if person and not active_person_role(db, person, names, p):
            raise BusinessError("У назначенного участника нет действующей роли в области проекта.")
    if p.client_binding_id:
        b = db.get(Binding, p.client_binding_id)
        if (
            not b
            or b.bot_id != p.bot_id
            or b.purpose != "client"
            or not b.active
            or not b.verified
            or b.partner_id != p.partner_id
            or b.brand_id != p.brand_id
            or b.establishment_id not in {None, p.establishment_id}
        ):
            raise BusinessError("Клиентский маршрут не соответствует проекту.")
    db.add(p)
    db.flush()
    for name in ("curator", "marketing"):
        db.add(ProjectLane(project_id=p.id, name=name, version=1, data={}))
    for code, item in CATALOG.items():
        db.add(
            ProjectBlock(
                project_id=p.id,
                code=code,
                lane=item["lane"],
                mode=default_mode(item, scenario),
                status="waiting",
                version=1,
                data={"template": deepcopy(item)},
            )
        )
    revision(db, actor, p, "curator", 0, {}, {"scenario": scenario, "matrix_version": VERSION}, "Создание проекта")
    db.flush()
    advance(db, p)
    return summary(db, p)


def ready(db, b):
    if b.mode == "not_applicable":
        return True
    if b.mode == "inherit":
        return bool(b.data.get("inheritance"))
    w = db.get(Work, b.work_id) if b.work_id else None
    if b.code == "community" and w and w.outcome != "cancelled" and w.data.get("community_accepted"):
        return True
    return bool(w and w.status == "done" and w.outcome == "accepted")


def prerequisite_ready(db, block, consumer):
    if block.code == "start_smm" and consumer == "readiness" and block.work_id:
        work = db.get(Work, block.work_id)
        return bool(
            work
            and work.outcome != "cancelled"
            and work.data.get("smm_agreement")
            and work.data.get("community_accepted")
        )
    return ready(db, block)


def inherited(db, p, b, data, actor):
    if b.code == "fp_training":
        raise BusinessError("Личное ознакомление подтверждается только реальным прогрессом того же человека.")
    source = data.get("source_work_id")
    if source:
        w = db.get(Work, source)
        if (
            not w
            or w.kind not in {"project." + b.code, "marketing." + b.code, b.code}
            or w.status != "done"
            or w.outcome != "accepted"
            or w.partner_id != p.partner_id
            or w.establishment_id != p.establishment_id
            or w.brand_id not in {None, p.brand_id}
            or not access.can_work(db, actor, w)
        ):
            raise BusinessError("Нужен доступный принятый результат того же предприятия и области.")
        return {"source_work_id": w.id, "version": w.version, "confirmed_by": actor.person_id, "at": now().isoformat()}
    if not data.get("evidence"):
        raise BusinessError("Укажите принятый результат или проверенный внешний документ и причину наследования.")
    return {"evidence": data["evidence"], "confirmed_by": actor.person_id, "at": now().isoformat()}


def configure(db, actor, project_id, common_version, data):
    p = get(db, project_id, True)
    versioned(p, common_version)
    lane = data["lane"]
    if lane not in {"curator", "marketing"}:
        raise BusinessError("Неизвестный контур.")
    authorize(db, actor, p, lane)
    row = db.scalar(
        select(ProjectLane).where(ProjectLane.project_id == p.id, ProjectLane.name == lane).with_for_update()
    )
    versioned(row, data["lane_version"])
    group = data["group"]
    if group not in GROUPS:
        raise BusinessError("Группа не найдена.")
    selected = [
        b
        for b in blocks(db, p).values()
        if b.lane == lane
        and CATALOG[b.code]["group"] == group
        and (not data.get("block_code") or b.code == data["block_code"])
    ]
    if not selected:
        raise AccessDenied("Группа принадлежит другому контуру.")
    mode = data["mode"]
    if mode not in MODES:
        raise BusinessError("Выберите режим.")
    before = {b.code: {"mode": b.mode, "data": deepcopy(b.data)} for b in selected}
    if p.scenario == "existing" and group == "followup" and mode != "not_applicable":
        raise BusinessError("Действующий объект не получает новый стартовый цикл. Создайте отдельную кампанию.")
    for b in selected:
        if (
            p.scenario == "existing"
            and b.code in {"internal_opening", "technical_opening", "official_opening"}
            and mode != "not_applicable"
        ):
            raise BusinessError("Действующий объект не получает фиктивное открытие.")
        w = db.get(Work, b.work_id) if b.work_id else None
        if w:
            if mode != b.mode or any(data.get(k) and data[k] != b.data.get(k) for k in ("route_key", "control_days")):
                raise BusinessError(
                    "Запущенная работа сохраняется. Используйте согласование изменения в карточке блока."
                )
            continue
        if mode == "inherit":
            proof = inherited(db, p, b, data, actor)
        else:
            proof = None
        if mode == "not_applicable" and not data.get("reason"):
            raise BusinessError("Для исключения укажите причину применимости.")
        b.mode = mode
        b.data = {
            **b.data,
            **{k: data[k] for k in ("route_key", "control_days", "reason") if data.get(k)},
            "inheritance": proof,
        }
        b.version += 1
    revision(
        db,
        actor,
        p,
        lane,
        row.version,
        before,
        {b.code: {"mode": b.mode, "data": b.data} for b in selected},
        data.get("reason") or "Короткий фильтр применимости",
    )
    row.version += 1
    db.flush()
    advance(db, p)
    return summary(db, p)


def set_profile(db, actor, project_id, version, data):
    p = get(db, project_id, True)
    versioned(p, version)
    authorize(db, actor, p, "curator")
    before = deepcopy(p.profile)
    for field, names in [("marketing_manager_id", {"marketing_manager"}), ("partner_person_id", {"partner"})]:
        if data.get(field) and data[field] != getattr(p, field):
            if not active_person_role(db, data[field], names, p):
                raise BusinessError("У нового участника нет действующего назначения в области проекта.")
            if any(
                b.work_id
                and db.get(Work, b.work_id).status != "done"
                and (b.lane == "marketing" if field == "marketing_manager_id" else True)
                for b in blocks(db, p).values()
            ):
                raise BusinessError(
                    "Сначала завершите передачу действующих работ; изменение участника не передаёт их автоматически."
                )
            setattr(p, field, data[field])
    flags = dict(p.profile.get("flags", {}))
    for k in (
        "own_delivery",
        "stamp",
        "mercury_help",
        "insurance",
        "psn",
        "instructor",
        "event",
        "online_acquiring",
        "startup_cycle",
        "marketing_followup",
        "campaign",
    ):
        if k in data:
            if data[k] not in {"yes", "no", "unknown"}:
                raise BusinessError("Укажите да / нет / неизвестно.")
            if p.scenario == "existing" and k in {"startup_cycle", "marketing_followup"} and data[k] == "yes":
                raise BusinessError("Для действующего объекта используйте отдельный маркетинговый план без сброса D0.")
            if flags.get(k) != data[k] and any(
                b.work_id and CATALOG[b.code].get("flag") == k for b in blocks(db, p).values()
            ):
                raise BusinessError("Условие уже используется работой: сначала согласуйте её изменение.")
            flags[k] = data[k]
    p.profile = {**p.profile, "flags": flags}
    revision(db, actor, p, "curator", p.version, before, p.profile, data.get("reason") or "Уточнение применимости")
    p.version += 1
    db.flush()
    advance(db, p)
    return summary(db, p)


def date_action(db, actor, project_id, version, data):
    p = get(db, project_id, True)
    versioned(p, version)
    authorize(db, actor, p, "curator")
    name = data["date_kind"]
    if name not in DATE_KEYS:
        raise BusinessError("Неизвестная дата.")
    value = parse_date(data["date"])
    if name.startswith("actual_"):
        if p.scenario == "existing":
            raise BusinessError("Исторические даты действующего предприятия не запускают новый цикл.")
        if value > now() or not data.get("evidence"):
            raise BusinessError("Фактическая дата не может быть будущей; укажите основание.")
        if p.dates.get(name):
            raise BusinessError("Фактическое событие уже зафиксировано; история не сбрасывается.")
        if name == "actual_technical" and p.scenario != "technical":
            bs = blocks(db, p)
            if not ready(db, bs["internal_opening"]) or bs["internal_opening"].mode == "not_applicable":
                raise BusinessError("Сначала примите внутреннее тестирование.")
        if name == "actual_official" and not p.dates.get("actual_technical"):
            raise BusinessError("Сначала зафиксируйте фактические первые продажи.")
        if name != "actual_event" and db.scalar(
            select(ProjectRemark.id).where(
                ProjectRemark.project_id == p.id, ProjectRemark.severity == "red", ProjectRemark.status == "open"
            )
        ):
            raise BusinessError("Открыто блокирующее красное замечание.")
        if name == "actual_internal" and (
            blocks(db, p)["readiness"].mode == "not_applicable" or not ready(db, blocks(db, p)["readiness"])
        ):
            raise BusinessError("Сводные условия подготовки ещё не приняты.")
    else:
        if value <= now():
            raise BusinessError("Плановая дата должна быть в будущем.")
    if name.startswith("planned_") and p.dates.get(name) != value.isoformat() + "+00:00":
        affected = [
            b
            for b in blocks(db, p).values()
            if CATALOG[b.code].get("anchor") == name
            and b.work_id
            and db.get(Work, b.work_id).status != "done"
            and db.get(Work, b.work_id).due_at != value
        ]
        if affected:
            raise BusinessError(
                "От этой даты уже запущена работа. Сначала согласуйте перенос её срока отдельно; дата не меняет активные обязательства автоматически."
            )
    dates = {**p.dates, name: value.isoformat() + "+00:00"}
    for prefix in ("planned", "actual"):
        ordered = [
            parse_date(dates[f"{prefix}_{k}"])
            for k in ("internal", "technical", "official")
            if dates.get(f"{prefix}_{k}")
        ]
        if ordered != sorted(ordered):
            raise BusinessError("Даты должны идти: внутреннее → техническое → официальное открытие.")
    revision(
        db,
        actor,
        p,
        "curator",
        p.version,
        p.dates,
        dates,
        data.get("evidence") or data.get("reason") or "Изменение плана",
    )
    p.dates = dates
    if name == "planned_technical":
        from ama.sla import case_for,opening_check
        for work in db.scalars(select(Work).where(Work.project_id==p.id,Work.kind=="digital",Work.status!="done")):
            case=case_for(db,work)
            if case:
                opening_check(db,case,work)
    p.version += 1
    if name == "actual_internal":
        p.stage = "internal_testing"
    elif name == "actual_technical":
        p.stage = "technical"
    elif name == "actual_official":
        p.stage = "enhanced_support"
    db.flush()
    advance(db, p)
    return summary(db, p)


def route_for(db, p, b):
    item = CATALOG[b.code]
    key = b.data.get("route_key") or item["route"]
    r = db.scalar(select(Record).where(Record.kind == "route", Record.key == key, Record.published.is_(True)))
    if not r:
        return None, "Не опубликован маршрут " + key
    d = r.data
    binding = db.get(Binding, d.get("binding_id")) if d.get("binding_id") else None
    if (
        not binding
        or binding.bot_id != p.bot_id
        or not binding.active
        or not binding.verified
        or binding.purpose == "client"
    ):
        return None, "Маршрут отключён или не проверен"
    if any(
        getattr(binding, k) and getattr(binding, k) != getattr(p, k)
        for k in ("partner_id", "brand_id", "establishment_id")
    ):
        return None, "Маршрут не соответствует области проекта"
    cal = db.scalar(
        select(Record).where(Record.kind == "calendar", Record.key == d.get("calendar"), Record.published.is_(True))
    )
    days = b.data.get("control_days") or d.get("control_workdays")
    if not cal or not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
        return None, "Нужны опубликованный календарь и контрольный срок 1–365 рабочих дней"
    owner = d.get("owner_id")
    reviewer = p.marketing_manager_id if b.lane == "marketing" else p.curator_id
    scope = SimpleNamespace(
        partner_id=p.partner_id, establishment_id=p.establishment_id, brand_id=None if item["shared"] else p.brand_id
    )
    if (
        not owner
        or not active_person_role(db, owner, STAFF, scope)
        or not reviewer
        or not active_person_role(
            db, reviewer, {"marketing_manager"} if b.lane == "marketing" else {"curator", "support_head"}, scope
        )
    ):
        return None, "Не назначен действующий исполнитель или принимающий"
    review_bindings = list(
        db.scalars(
            select(Binding).where(
                Binding.bot_id == p.bot_id,
                Binding.purpose == ("marketing" if b.lane == "marketing" else "support"),
                Binding.active.is_(True),
                Binding.verified.is_(True),
            )
        )
    )
    review = next(
        (
            x
            for x in review_bindings
            if all(
                not getattr(x, k) or getattr(x, k) == getattr(scope, k)
                for k in ("partner_id", "brand_id", "establishment_id")
            )
        ),
        None,
    )
    if not review:
        return None, "Нет проверенного маршрута принимающего"
    return (binding, owner, reviewer, review, Calendar(cal.data), days, d["calendar"]), None


def launch(db, p, b, route, origin=None, title=None):
    binding, owner, reviewer, review, cal, days, calendar = route
    item = CATALOG[b.code]
    due = cal.end_of_workday(now(), days)
    anchor = item.get("anchor")
    if anchor and anchor.startswith("planned_") and p.dates.get(anchor):
        scheduled = parse_date(p.dates[anchor]) + timedelta(days=item.get("offset", 0))
        if scheduled > now():
            due = scheduled
    if b.code == "readiness" and p.dates.get("planned_technical"):
        scheduled = parse_date(p.dates["planned_technical"]) - timedelta(days=5)
        if scheduled > now():
            due = scheduled
    w = Work(
        kind="digital" if b.code == "digital" else ("marketing." if b.lane == "marketing" else "project.") + b.code,
        title=title or item["title"],
        partner_id=p.partner_id,
        establishment_id=p.establishment_id,
        brand_id=None if item["shared"] else p.brand_id,
        owner_id=owner,
        executor_id=owner,
        reviewer_id=reviewer,
        binding_id=binding.id,
        review_binding_id=review.id,
        client_binding_id=None if item["shared"] else p.client_binding_id,
        partner_person_id=p.partner_person_id,
        project_id=p.id,
        origin_key=origin or f"plan:{b.id}",
        due_at=due,
        original_due_at=due,
        data={"calendar": calendar, "project_block_id": b.id, "mode": b.mode, "section": item["section"]},
    )
    db.add(w)
    db.flush()
    from ama.sla import initialize
    fixed_date=bool((item.get("anchor", "").startswith("planned_") and p.dates.get(item["anchor"])) or (b.code=="readiness" and p.dates.get("planned_technical")) or (origin and origin.startswith("campaign:")))
    initialize(db, w, explicit_due=fixed_date)
    make_timer(db, w, "deadline", w.due_at)
    stamp(db, None, w, "PROJECT_WORK_CREATED", data={"project_id": p.id, "block_id": b.id})
    queue(
        db,
        binding,
        "created:" + w.id,
        "Новая работа: " + w.title + "\nID: " + w.id,
        work=w,
        buttons=[["Открыть", "af:w:" + w.id]],
    )
    return w


def advance(db, p):
    """Only eligible tasks are born; absent dates/routes never create past deadlines."""
    bs = blocks(db, p)
    changed = False
    for b in bs.values():
        old = (b.status, deepcopy(b.data))
        item = CATALOG[b.code]
        reason = None
        if b.work_id:
            w = db.get(Work, b.work_id)
            b.status = "cancelled" if w and w.outcome == "cancelled" else "accepted" if ready(db, b) else "active"
        elif b.mode == "not_applicable":
            b.status = "not_applicable"
        elif b.mode == "inherit":
            b.status = "inherited" if ready(db, b) else "waiting"
            reason = None if ready(db, b) else "Нужен проверенный источник наследования"
        else:
            flag = item.get("flag")
            if flag and p.profile.get("flags", {}).get(flag, "unknown") != "yes":
                reason = "Условие " + flag + ": " + p.profile.get("flags", {}).get(flag, "unknown")
            deps = [k for k in item["depends_on"] if not prerequisite_ready(db, bs[k], b.code)]
            if deps:
                reason = "Непринятые зависимости: " + ", ".join(deps)
            anchor = item.get("anchor")
            if anchor:
                value = p.dates.get(anchor)
                if not value:
                    reason = "Не определена дата " + anchor
                elif anchor.startswith("actual_") and parse_date(value) + timedelta(days=item.get("offset", 0)) > now():
                    reason = (
                        "Ожидается контрольная дата "
                        + (parse_date(value) + timedelta(days=item.get("offset", 0))).isoformat()
                        + " UTC"
                    )
            if b.code in {"internal_opening", "technical_opening", "official_opening"}:
                date = "actual_" + b.code.removesuffix("_opening")
                if not p.dates.get(date):
                    reason = "Нужно реальное событие " + date + " и его основание"
            if reason:
                b.status = "waiting"
            else:
                route, reason = route_for(db, p, b)
                if not reason:
                    if item["shared"]:
                        # Serialize all physical tasks across projects/brands at this establishment.
                        db.scalar(select(Entity).where(Entity.id == p.establishment_id).with_for_update())
                        shared_key = f"{p.establishment_id}:{b.code}"
                        shared = db.scalar(
                            select(FacilityResult).where(FacilityResult.active_key == shared_key).with_for_update()
                        )
                        if shared and shared.work_id:
                            existing = db.get(Work, shared.work_id)
                            if (
                                b.mode == "verify"
                                and existing
                                and existing.status == "done"
                                and existing.outcome == "accepted"
                            ):
                                w = launch(db, p, b, route)
                                w.data = {**w.data, "previous_result_id": existing.id}
                                shared.data = {
                                    **shared.data,
                                    "previous_results": shared.data.get("previous_results", []) + [existing.id],
                                }
                                shared.work_id = w.id
                                shared.version += 1
                                b.work_id = w.id
                            else:
                                b.work_id = shared.work_id
                        else:
                            w = launch(db, p, b, route)
                            b.work_id = w.id
                            if shared:
                                shared.work_id = w.id
                            else:
                                db.add(
                                    FacilityResult(
                                        establishment_id=p.establishment_id,
                                        code=b.code,
                                        active_key=shared_key,
                                        work_id=w.id,
                                        data={},
                                    )
                                )
                    else:
                        b.work_id = launch(db, p, b, route).id
                        if (
                            b.code == "community"
                            and bs["start_smm"].mode in {"perform", "verify"}
                            and not bs["start_smm"].work_id
                        ):
                            bs["start_smm"].work_id = b.work_id
                            package = db.get(Work, b.work_id)
                            package.title = "Сообщество и стартовое SMM: " + p.title
                    b.status = "accepted" if ready(db, b) else "active"
                    changed = True
                else:
                    b.status = "waiting"
        b.data = {**b.data, "waiting_reason": reason}
        if old != (b.status, b.data):
            changed = True
    p.profile = {**p.profile, "next_scan": (now() + timedelta(seconds=30)).isoformat()}
    db.flush()
    return changed


def before_transition(db, actor, w, action, data):
    linked = list(db.scalars(select(ProjectBlock).where(ProjectBlock.work_id == w.id)))
    if not linked:
        return
    if action in {"cancel", "reschedule", "handoff"} and (
        action == "cancel" or w.data.get("procurement_ordered") or w.data.get("paid_started_at")
    ):
        raise BusinessError(
            "Проектная работа отменяется только через согласование изменения; история и оплаты сохраняются."
        )
    if action not in {"submit", "accept"}:
        return
    for b in linked:
        if b.mode == "not_applicable":
            continue
        p = get(db, b.project_id)
        bs = blocks(db, p)
        if any(not prerequisite_ready(db, bs[k], b.code) for k in CATALOG[b.code]["depends_on"]):
            raise BusinessError("Изменились зависимости проекта; результат пока нельзя принять.")
        if b.code == "readiness":
            for required in ("fp_training", "guest_care", "franchise"):
                if bs[required].mode == "not_applicable" or not ready(db, bs[required]):
                    raise BusinessError("Обязательное условие подготовки не выполнено: " + required)
        if b.code in {"readiness", "internal_opening", "technical_opening", "official_opening"}:
            if db.scalar(
                select(ProjectRemark.id).where(
                    ProjectRemark.project_id == p.id, ProjectRemark.severity == "red", ProjectRemark.status == "open"
                )
            ):
                raise BusinessError("Есть открытые блокирующие замечания.")
        if b.code == "fp_training":
            course = db.scalar(
                select(Record).where(Record.kind == "course", Record.key == "general", Record.published.is_(True))
            )
            training = (
                db.scalar(
                    select(Training).where(
                        Training.person_id == p.partner_person_id,
                        Training.course_key == "general",
                        Training.course_version == course.version,
                        Training.completed_at.is_not(None),
                    )
                )
                if course and p.partner_person_id
                else None
            )
            if not training:
                raise BusinessError("Назначенный ФП не завершил опубликованный курс лично.")
        if b.code == "start_smm":
            end = w.data.get("smm_until")
            if not end or parse_date(end) > now() or not w.data.get("smm_report"):
                raise BusinessError(
                    "Подготовка SMM не завершает будущий период. Нужны окончание согласованного периода и отчёт."
                )
        if b.code in {"kitchen_equipment", "systems_equipment"} and not w.data.get("procurement_received"):
            raise BusinessError("Оплата не равна получению. Нужна подтверждённая полная поставка.")


def summary(db, p):
    rows = blocks(db, p)
    return {
        "id": p.id,
        "version": p.version,
        "title": p.title,
        "scenario": p.scenario,
        "stage": p.stage,
        "blocks": len(rows),
        "accepted": sum(b.status in {"accepted", "inherited"} for b in rows.values()),
        "waiting": sum(b.status == "waiting" for b in rows.values()),
        "works": len({b.work_id for b in rows.values() if b.work_id}),
    }


def request_change(db, actor, project_id, version, data, key):
    p = get(db, project_id, True)
    versioned(p, version)
    b = db.get(ProjectBlock, data["block_id"])
    if not b or b.project_id != p.id or not b.work_id:
        raise BusinessError("Выберите запущенный блок проекта.")
    authorize(db, actor, p, b.lane)
    w = db.scalar(select(Work).where(Work.id == b.work_id).with_for_update())
    if w.status == "done":
        raise BusinessError("Завершённая история не меняется. Создайте отдельную работу.")
    if (
        data.get("decision") not in {"cancel", "continue", "reschedule"}
        or not data.get("reason")
        or not data.get("settlement")
    ):
        raise BusinessError("Выберите продолжить или отменить и укажите причину, условия по оплатам и заказам.")
    if data["decision"] == "reschedule" and (not data.get("due_at") or parse_date(data["due_at"]) <= now()):
        raise BusinessError("Для переноса укажите будущий согласованный срок.")
    old = db.get(ProjectChange, key)
    if old:
        return {"id": old.id, "version": old.version, "status": old.status}
    # Every project referencing a common physical work participates in cancellation.
    required = {w.executor_id: "executor"}
    for other in db.scalars(select(ProjectBlock).where(ProjectBlock.work_id == w.id)):
        plan = get(db, other.project_id)
        required[plan.marketing_manager_id if other.lane == "marketing" else plan.curator_id] = (
            "marketing_manager" if other.lane == "marketing" else "curator"
        )
        if w.data.get("paid_started_at") or w.data.get("procurement_ordered") or w.data.get("offer_accepted"):
            if not plan.partner_person_id:
                raise BusinessError("Для оплаченной или заказанной работы сначала назначьте ФП.")
            required[plan.partner_person_id] = "partner"
    if None in required:
        raise BusinessError("Не назначены все согласующие.")
    change = ProjectChange(
        id=key,
        project_id=p.id,
        block_id=b.id,
        work_id=w.id,
        work_version=w.version,
        requested_by=actor.person_id,
        data={**data, "required": required},
        approvals={},
        version=1,
    )
    db.add(change)
    db.flush()
    stamp(db, actor, change, "PROJECT_CHANGE_REQUESTED", data={"work_id": w.id, "required": required})
    related = [get(db, row.project_id) for row in db.scalars(select(ProjectBlock).where(ProjectBlock.work_id == w.id))]
    for person, role in required.items():
        if role == "partner":
            owner_plan = next((x for x in related if x.partner_person_id == person), None)
            target = (
                db.get(Binding, owner_plan.client_binding_id) if owner_plan and owner_plan.client_binding_id else None
            )
        elif role == "executor":
            target = db.get(Binding, w.binding_id)
        else:
            purpose = "marketing" if role == "marketing_manager" else "support"
            target = next(
                (
                    x
                    for x in db.scalars(
                        select(Binding).where(
                            Binding.bot_id == p.bot_id,
                            Binding.purpose == purpose,
                            Binding.active.is_(True),
                            Binding.verified.is_(True),
                        )
                    )
                    if all(
                        not getattr(x, k) or getattr(x, k) == getattr(p, k)
                        for k in ("partner_id", "brand_id", "establishment_id")
                    )
                ),
                None,
            )
        if not target or not target.active or not target.verified:
            raise BusinessError("Нет доступного маршрута согласующего. Подключите чат до отправки согласования.")
        queue(
            db,
            target,
            f"project-change:{change.id}:{person}",
            f"Требуется согласование: {w.title}\nРешение: {data['decision']}\nПричина: {data['reason']}\nУсловия расчётов: {data['settlement']}\nОткройте /projects → проект → согласование.",
            buttons=[["Мои проекты", "af:projects"]],
        )
    return {"id": change.id, "version": change.version, "required": required}


def approve_change(db, actor, change_id, version, data):
    c = db.scalar(select(ProjectChange).where(ProjectChange.id == change_id).with_for_update())
    if not c:
        raise BusinessError("Согласование не найдено.")
    versioned(c, version)
    get(db, c.project_id, True)
    w = db.scalar(select(Work).where(Work.id == c.work_id).with_for_update())
    versioned(w, c.work_version)
    if c.status != "requested" or actor.person_id not in c.data["required"]:
        raise AccessDenied("Нет открытого согласования для этого пользователя.")
    role = c.data["required"][actor.person_id]
    if role == "partner":
        binding = db.get(Binding, actor.binding_id)
        related = [get(db, b.project_id) for b in db.scalars(select(ProjectBlock).where(ProjectBlock.work_id == w.id))]
        if not any(binding.id == x.client_binding_id and x.partner_person_id == actor.person_id for x in related):
            raise AccessDenied("ФП подтверждает только из подключённого клиентского чата проекта.")
        from ama.auth import require

        require(db, actor, {"partner"})
    elif role == "executor":
        from ama.auth import require

        require(db, actor, STAFF, w)
    else:
        # An approver may belong to another plan sharing this physical work.
        plans = [get(db, b.project_id) for b in db.scalars(select(ProjectBlock).where(ProjectBlock.work_id == w.id))]
        plan = next((x for x in plans if actor.person_id in {x.curator_id, x.marketing_manager_id}), None)
        if not plan:
            raise AccessDenied("Назначение согласующего изменилось.")
        authorize(db, actor, plan, "marketing" if role == "marketing_manager" else "curator")
    from ama.ui.access import grants_hash

    if not data.get("approve"):
        c.status = "rejected"
    else:
        c.approvals = {
            **c.approvals,
            actor.person_id: {"binding": actor.binding_id, "grants": grants_hash(db, actor), "at": now().isoformat()},
        }
        if set(c.approvals) == set(c.data["required"]):
            from ama.auth import Actor

            for person, proof in c.approvals.items():
                if grants_hash(db, Actor(person, proof["binding"])) != proof["grants"]:
                    raise Conflict("Назначения согласующего изменились. Создайте актуальное согласование.")
            if c.data["decision"] == "cancel":
                w.status, w.outcome = "done", "cancelled"
                w.version += 1
                w.data = {**w.data, "agreed_change": c.id, "cancellation_reason": c.data["reason"]}
                for t in db.scalars(select(Timer).where(Timer.work_id == w.id, Timer.status == "pending")):
                    t.status = "cancelled"
                for b in db.scalars(select(ProjectBlock).where(ProjectBlock.work_id == w.id)):
                    b.mode = "not_applicable"
                    b.status = "cancelled"
                    b.version += 1
                shared = db.scalar(
                    select(FacilityResult).where(FacilityResult.work_id == w.id, FacilityResult.active_key.is_not(None))
                )
                if shared:
                    shared.active_key = None
                stamp(
                    db,
                    actor,
                    w,
                    "PROJECT_CANCELLATION_AGREED",
                    data={"change_id": c.id, "financial_settlement": c.data.get("settlement")},
                )
            elif c.data["decision"] == "reschedule":
                due = parse_date(c.data["due_at"])
                if due <= now():
                    raise BusinessError("Согласованный срок уже прошёл. Создайте новое согласование.")
                w.due_at = due
                w.version += 1
                w.data = {**w.data, "agreed_change": c.id, "deadline_change": {"reason": c.data["reason"]}}
                for t in db.scalars(select(Timer).where(Timer.work_id == w.id, Timer.status == "pending")):
                    t.status = "cancelled"
                make_timer(db, w, "deadline", due)
                stamp(db, actor, w, "PROJECT_RESCHEDULE_AGREED", data={"change_id": c.id})
            c.status = "approved"
    c.version += 1
    stamp(db, actor, c, "PROJECT_CHANGE_DECISION", data={"status": c.status, "approve": bool(data.get("approve"))})
    return {"id": c.id, "version": c.version, "status": c.status}


def remark(db, actor, project_id, version, data):
    p = get(db, project_id, True)
    versioned(p, version)
    authorize(db, actor, p, "curator")
    if data.get("remark_id"):
        r = db.get(ProjectRemark, data["remark_id"])
        if not r or r.project_id != p.id or not data.get("evidence"):
            raise BusinessError("Для закрытия нужны замечание этого проекта и основание устранения.")
        versioned(r, data["remark_version"])
        r.status = "closed"
        r.data = {**r.data, "closed_by": actor.person_id, "evidence": data["evidence"]}
        r.version += 1
    else:
        if data.get("severity") not in {"red", "yellow", "green"} or not data.get("text"):
            raise BusinessError("Выберите критичность и укажите замечание.")
        if not active_person_role(db, data["owner_id"], STAFF | {"partner"}, p):
            raise BusinessError("Не назначен ответственный за устранение.")
        due = parse_date(data["due_at"])
        if due <= now():
            raise BusinessError("Срок устранения должен быть будущим.")
        r = ProjectRemark(
            project_id=p.id,
            owner_id=data["owner_id"],
            severity=data["severity"],
            text=data["text"],
            due_at=due,
            data={},
        )
        db.add(r)
        db.flush()
    p.version += 1
    stamp(db, actor, r, "PROJECT_REMARK_CHANGED", data={"project_id": p.id, "status": r.status or "open"})
    return {"id": r.id, "version": r.version, "status": r.status or "open"}


def campaign(db, actor, project_id, version, data, key):
    p = get(db, project_id, True)
    versioned(p, version)
    authorize(db, actor, p, "marketing")
    start = parse_date(data["start_at"])
    end = parse_date(data["until_at"])
    if start <= now() or end < start or not data.get("plan_reference"):
        raise BusinessError("Нужны будущий старт, конец периода и ссылка на согласованный план.")
    if data["interval"] not in {"once", "monthly"}:
        raise BusinessError("Неизвестная периодичность.")
    c = ProjectCampaign(
        id=key,
        project_id=p.id,
        title=data["title"],
        owner_id=actor.person_id,
        next_at=start,
        until_at=end,
        data={**data, "day": start.day, "route_key": data.get("route_key") or "marketing"},
    )
    if not db.get(ProjectCampaign, key):
        db.add(c)
        db.flush()
    return {
        "id": key,
        "next_at": start.isoformat(),
        "message": "Внутренние объектные работы; массовая рассылка не запускается.",
    }


def tick(db):
    current = now()
    p = db.scalar(
        select(ProjectPlan)
        .where(ProjectPlan.bot_id == settings().bot_id, ProjectPlan.next_at <= current)
        .order_by(ProjectPlan.next_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if p:
        changed = advance(db, p)
        p.next_at = current + timedelta(seconds=30)
        return changed
    c = db.scalar(
        select(ProjectCampaign)
        .where(ProjectCampaign.enabled.is_(True), ProjectCampaign.next_at <= current)
        .order_by(ProjectCampaign.next_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if c:
        if c.until_at < current:
            c.enabled = False
            c.data = {**c.data, "waiting_reason": "Период кампании завершён; прошлые циклы не создаются"}
            return True
        p = get(db, c.project_id, True)
        b = blocks(db, p)["campaign"]
        override = SimpleNamespace(
            code=b.code,
            lane=b.lane,
            data={**b.data, "route_key": c.data["route_key"], "control_days": c.data.get("control_days")},
        )
        route, reason = route_for(db, p, override)
        if (
            reason
            or c.owner_id != p.marketing_manager_id
            or not active_person_role(db, c.owner_id, {"marketing_manager"}, p)
        ):
            c.data = {**c.data, "waiting_reason": reason or "Снято назначение менеджера"}
            c.next_at = current + timedelta(minutes=5)
            return True
        origin = f"campaign:{c.id}:{c.data.get('cycle', 0)}"
        if not db.scalar(select(Work.id).where(Work.origin_key == origin)):
            w = launch(db, p, b, route, origin, c.title)
            w.data = {**w.data, "campaign_id": c.id, "plan_reference": c.data["plan_reference"]}
        if c.data["interval"] == "once":
            c.enabled = False
        else:
            scheduled = parse_date(c.data.get("scheduled_at") or c.data["start_at"])
            while scheduled <= current:
                year = scheduled.year + (scheduled.month == 12)
                month = 1 if scheduled.month == 12 else scheduled.month + 1
                scheduled = scheduled.replace(
                    year=year, month=month, day=min(c.data["day"], monthrange(year, month)[1])
                )
            c.next_at = scheduled
            c.enabled = scheduled <= c.until_at
            c.data = {
                **c.data,
                "scheduled_at": scheduled.isoformat() + "+00:00",
                "cycle": c.data.get("cycle", 0) + 1,
                "waiting_reason": None,
            }
        return True
    return False


def work_details(db, actor, work_id, version, data):
    """Typed evidence; no arbitrary JSON patching or automatic acceptance."""
    from ama.auth import require

    w = db.scalar(select(Work).where(Work.id == work_id).with_for_update())
    if not w:
        raise BusinessError("Работа не найдена.")
    versioned(w, version)
    require(db, actor, STAFF | {"partner"}, w)
    linked = db.scalar(select(ProjectBlock).where(ProjectBlock.work_id == w.id))
    if not linked or w.status == "done":
        raise BusinessError("Нет открытой проектной работы.")
    action = data["step"]
    before = deepcopy(w.data)
    if linked.code in {"kitchen_equipment", "systems_equipment"}:
        state = deepcopy(w.data.get("procurement", {}))
        if state.get("received"):
            raise BusinessError(
                "Получение комплекта уже подтверждено. Исправление оформляется отдельным согласованием с сохранением акта."
            )
        if action == "specification":
            if actor.person_id != w.executor_id or not data.get("items"):
                raise AccessDenied("Спецификацию задаёт назначенный исполнитель.")
            if state.get("ordered"):
                raise BusinessError("Заказанная спецификация меняется через отдельное согласование.")
            items = data["items"]
            if len({i["key"] for i in items}) != len(items) or any(
                not i.get("supplier")
                or not i.get("model")
                or not isinstance(i.get("quantity"), int)
                or i["quantity"] < 1
                or not isinstance(i.get("price_minor"), int)
                or i["price_minor"] < 0
                or not i.get("delivery_at")
                for i in items
            ):
                raise BusinessError(
                    "Нужны уникальные позиции, модель, поставщик, количество, цена в копейках и дата поставки."
                )
            for i in items:
                if parse_date(i["delivery_at"]) <= now():
                    raise BusinessError("Плановая поставка должна быть будущей.")
            state = {
                "items": items,
                "spec_version": state.get("spec_version", 0) + 1,
                "approved": False,
                "ordered": False,
                "receipts": {},
            }
        elif action in {"approve_specification", "ordered", "receipt", "confirm_receipt"}:
            # Shared tasks have no brand-specific client Work: curator records the named FP's
            # signed evidence. This does not grant the curator a partner role.
            p = get(db, linked.project_id)
            if not p.partner_person_id or not active_person_role(db, p.partner_person_id, {"partner"}, p):
                raise BusinessError("Сначала назначьте действующего уполномоченного ФП.")
            if actor.person_id == p.partner_person_id:
                require(db, actor, {"partner"}, w)
            elif actor.person_id == p.curator_id:
                require(db, actor, {"curator"}, w)
                if not data.get("evidence"):
                    raise BusinessError("Нужно основание явного подтверждения назначенным ФП.")
            else:
                raise AccessDenied("Решение фиксирует назначенный ФП либо куратор с его подтверждением.")
            if not state.get("items"):
                raise BusinessError("Сначала подготовьте спецификацию.")
            if action == "approve_specification":
                state["approved"] = {
                    "by": actor.person_id,
                    "partner_id": p.partner_person_id,
                    "evidence": data.get("evidence"),
                    "version": state["spec_version"],
                }
            elif action == "ordered":
                if not state.get("approved") or not data.get("evidence"):
                    raise BusinessError("Нужны согласованная спецификация и подтверждение заказа.")
                state["ordered"] = {"by": actor.person_id, "evidence": data["evidence"]}
            elif action == "receipt":
                if not state.get("ordered") or not data.get("evidence"):
                    raise BusinessError("Сначала подтвердите заказ; для получения нужно основание.")
                item = next((i for i in state["items"] if i["key"] == data.get("item_key")), None)
                count = data.get("received_quantity")
                if not item or not isinstance(count, int) or not 0 <= count <= item["quantity"]:
                    raise BusinessError("Укажите позицию и суммарно полученное количество в пределах заказа.")
                state["receipts"] = {
                    **state["receipts"],
                    item["key"]: {"quantity": count, "evidence": data["evidence"]},
                }
            else:
                if not state.get("ordered") or any(
                    state.get("receipts", {}).get(i["key"], {}).get("quantity", 0) != i["quantity"]
                    for i in state["items"]
                ):
                    raise BusinessError("Частичная поставка не закрывает комплект.")
                if not data.get("evidence"):
                    raise BusinessError("Нужно явное подтверждение полной приёмки ФП.")
                state["received"] = {"by": actor.person_id, "evidence": data["evidence"]}
        else:
            raise BusinessError("Неизвестный шаг закупки.")
        w.data = {
            **w.data,
            "procurement": state,
            "procurement_ordered": bool(state.get("ordered")),
            "procurement_received": bool(state.get("received")),
        }
    elif linked.code in {"community", "start_smm"} and action == "community_ready":
        if (
            actor.person_id != w.reviewer_id
            or not data.get("community_url")
            or not data.get("evidence")
            or not data.get("admin_access")
        ):
            raise AccessDenied(
                "Менеджер подтверждает готовое сообщество: ссылка, проверенные права и основание приёмки обязательны."
            )
        from urllib.parse import urlsplit

        url = urlsplit(data["community_url"])
        if url.scheme != "https" or url.hostname not in {"vk.com", "www.vk.com"} or not url.path.strip("/"):
            raise BusinessError("Нужна HTTPS-ссылка на готовое сообщество VK.")
        w.data = {
            **w.data,
            "community_accepted": {
                "by": actor.person_id,
                "url": data["community_url"],
                "evidence": data["evidence"],
                "admin_access": data["admin_access"],
            },
        }
    elif linked.code in {"community", "start_smm"} and action == "smm_period":
        if actor.person_id != w.reviewer_id:
            raise AccessDenied("Период и результат SMM фиксирует назначенный менеджер.")
        start = parse_date(data["start_at"])
        end = parse_date(data["until_at"])
        if end <= start or not all(data.get(k) for k in ("plan_reference", "volume", "guest_care_owner", "evidence")):
            raise BusinessError(
                "Нужны согласованные период, объём, контент-план, материалы, ответственный Службы заботы и основание."
            )
        if w.data.get("smm_agreement") and (
            w.data.get("smm_start") != data["start_at"] or w.data.get("smm_until") != data["until_at"]
        ):
            raise BusinessError(
                "Согласованный период уже действует. Изменение оформляется отдельно, история не переписывается."
            )
        if end > now() and w.status != "review":
            w.due_at = end
        w.data = {
            **w.data,
            "smm_start": data["start_at"],
            "smm_until": data["until_at"],
            "smm_report": data.get("report"),
            "smm_agreement": data,
        }
    else:
        raise BusinessError("Для этого пункта используйте результат работы и вложения.")
    w.version += 1
    for timer in db.scalars(select(Timer).where(Timer.work_id == w.id, Timer.status == "pending")):
        timer.status = "cancelled"
    make_timer(db, w, "deadline", w.due_at)
    stamp(
        db,
        actor,
        w,
        "PROJECT_EVIDENCE_RECORDED",
        old=w.version - 1,
        data={"step": action, "before": before, "after": w.data},
    )
    from ama.sla import refresh_deadline
    refresh_deadline(db,w,"project_evidence")
    return {"id": w.id, "version": w.version}
