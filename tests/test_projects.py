from datetime import timedelta
import pytest
from sqlalchemy import select, func
from ama.auth import Actor, roles_for
from ama.errors import BusinessError, AccessDenied, Conflict
from ama.models import (
    ProjectPlan,
    ProjectLane,
    ProjectRemark,
    ProjectCampaign,
    ProjectChange,
    EstablishmentBrand,
    Entity,
    Record,
    Work,
    Outbox,
    Timer,
    Role,
    now,
)
from ama import project_flow as flow
from ama.project_catalog import CATALOG
from ama.service import transition
from ama.ui import access, business, engine as ui


def plan(ctx, scenario="new", key="a" * 32, brand="brand"):
    if not ctx.db.scalar(
        select(EstablishmentBrand).where(
            EstablishmentBrand.establishment_id == "est", EstablishmentBrand.brand_id == brand
        )
    ):
        ctx.db.add(EstablishmentBrand(establishment_id="est", brand_id=brand))
        ctx.db.flush()
    flow.create(
        ctx.db,
        ctx.curator,
        {
            "title": "Запуск кафе",
            "partner_id": "partner",
            "brand_id": brand,
            "establishment_id": "est",
            "scenario": scenario,
            "marketing_manager_id": "marketing",
            "partner_person_id": "fp",
            "client_binding_id": "client" if brand == "brand" else None,
        },
        key,
    )
    return flow.get(ctx.db, key)


def route(ctx, key="support", owner="curator", purpose="support"):
    r = ctx.db.scalar(select(Record).where(Record.kind == "route", Record.key == key))
    data = {
        "binding_id": purpose,
        "owner_id": owner,
        "reviewer_id": "curator",
        "calendar": "support",
        "control_workdays": 2,
    }
    if r:
        r.data = data
    else:
        ctx.db.add(Record(kind="route", key=key, published=True, data=data))
    ctx.db.flush()


def single(ctx, p, code, mode="perform"):
    bs = flow.blocks(ctx.db, p)
    for b in bs.values():
        if not b.work_id:
            b.mode = "not_applicable"
    b = bs[code]
    b.mode = mode
    ctx.db.flush()
    return b


def accepted(ctx, p, code):
    b = flow.blocks(ctx.db, p)[code]
    b.mode = "inherit"
    b.data = {**b.data, "inheritance": {"evidence": "Проверено куратором"}}
    return b


@pytest.mark.parametrize("scenario", ["new", "additional_brand", "existing", "technical", "reconstruction"])
def test_matrix_all_scenarios_persistent_without_fake_dates(ctx, scenario):
    p = plan(ctx, scenario)
    assert len(flow.blocks(ctx.db, p)) == len(CATALOG) > 80
    assert p.dates == {}
    assert not ctx.db.scalar(select(Work.id).where(Work.project_id == p.id))
    ctx.db.commit()
    ctx.db.expire_all()
    assert flow.get(ctx.db, p.id).scenario == scenario
    if scenario == "existing":
        assert all(b.mode == "not_applicable" for b in flow.blocks(ctx.db, p).values() if b.code != "support")


def test_catalog_dependency_graph_is_complete_and_acyclic():
    seen = set()

    def visit(code, stack):
        assert code not in stack
        if code in seen:
            return
        for dep in CATALOG[code]["depends_on"]:
            assert dep in CATALOG
            visit(dep, stack | {code})
        seen.add(code)

    for code in CATALOG:
        visit(code, set())
    assert seen == set(CATALOG)


def test_future_blocks_wait_for_published_route_and_explicit_control(ctx):
    p = plan(ctx)
    b = single(ctx, p, "support")
    flow.advance(ctx.db, p)
    assert not b.work_id and "контрольный срок" in b.data["waiting_reason"]
    route(ctx)
    flow.advance(ctx.db, p)
    assert b.work_id
    w = ctx.db.get(Work, b.work_id)
    assert w.due_at > now() and w.original_due_at == w.due_at
    for _ in range(3):
        flow.advance(ctx.db, p)
    assert ctx.db.scalar(select(func.count()).select_from(Work).where(Work.project_id == p.id)) == 1


def test_independent_branches_start_without_waiting_for_design(ctx):
    route(ctx)
    p = plan(ctx)
    bs = flow.blocks(ctx.db, p)
    assert bs["support"].work_id and bs["fp_training"].work_id
    assert not bs["project_bundle"].work_id
    assert "dimensions" in bs["project_bundle"].data["waiting_reason"]


def test_physical_work_not_duplicated_across_brands(ctx):
    route(ctx, "registrations")
    p = plan(ctx)
    ctx.db.add(Entity(id="brand2", kind="brand", name="Второй бренд", data={}))
    ctx.db.flush()
    p2 = plan(ctx, "additional_brand", "b" * 32, "brand2")
    first = flow.blocks(ctx.db, p)["company"]
    second = flow.blocks(ctx.db, p2)["company"]
    second.mode = "verify"
    flow.advance(ctx.db, p2)
    assert first.work_id and first.work_id == second.work_id
    assert ctx.db.get(Work, first.work_id).brand_id is None
    assert second.mode == "verify"


def test_inheritance_requires_source_and_does_not_create_accepted_work(ctx):
    p = plan(ctx)
    lane = ctx.db.scalar(select(ProjectLane).where(ProjectLane.project_id == p.id, ProjectLane.name == "curator"))
    data = {
        "lane": "curator",
        "lane_version": lane.version,
        "group": "equipment",
        "mode": "inherit",
        "reason": "На площадке уже установлено",
    }
    with pytest.raises(BusinessError):
        flow.configure(ctx.db, ctx.curator, p.id, p.version, data)
    data["evidence"] = "Акт осмотра и комплектности №12, проверено куратором"
    flow.configure(ctx.db, ctx.curator, p.id, p.version, data)
    assert all(
        b.status == "inherited" and not b.work_id
        for b in flow.blocks(ctx.db, p).values()
        if CATALOG[b.code]["group"] == "equipment"
    )
    assert not ctx.db.scalar(select(Work.id).where(Work.project_id == p.id, Work.status == "done"))


def test_marketing_lane_does_not_change_curator_and_detects_stale_lane(ctx):
    p = plan(ctx)
    actor = Actor("marketing", "marketing")
    lane = ctx.db.scalar(select(ProjectLane).where(ProjectLane.project_id == p.id, ProjectLane.name == "marketing"))
    d = {
        "lane": "marketing",
        "lane_version": lane.version,
        "group": "event",
        "mode": "not_applicable",
        "reason": "Мероприятия нет",
    }
    flow.configure(ctx.db, actor, p.id, p.version, d)
    assert p.version == 1
    with pytest.raises(Conflict):
        flow.configure(ctx.db, actor, p.id, p.version, d)
    with pytest.raises(AccessDenied):
        flow.configure(ctx.db, actor, p.id, p.version, {**d, "lane": "curator", "group": "repair"})
    with pytest.raises(AccessDenied):
        flow.date_action(
            ctx.db,
            actor,
            p.id,
            p.version,
            {"date_kind": "planned_event", "date": (now() + timedelta(days=30)).isoformat() + "Z"},
        )


def test_existing_never_gets_new_d0_or_openings(ctx):
    p = plan(ctx, "existing")
    with pytest.raises(BusinessError):
        flow.date_action(
            ctx.db,
            ctx.curator,
            p.id,
            p.version,
            {
                "date_kind": "actual_technical",
                "date": (now() - timedelta(days=1)).isoformat() + "Z",
                "evidence": "Прошлая история",
            },
        )
    with pytest.raises(BusinessError):
        flow.set_profile(ctx.db, ctx.curator, p.id, p.version, {"startup_cycle": "yes"})
    assert p.dates == {}


def test_planned_dates_never_start_d90_and_event_is_independent(ctx):
    route(ctx)
    p = plan(ctx)
    b = single(ctx, p, "d90")
    p.profile = {**p.profile, "flags": {"startup_cycle": "yes"}}
    flow.date_action(
        ctx.db,
        ctx.curator,
        p.id,
        p.version,
        {"date_kind": "planned_technical", "date": (now() + timedelta(days=40)).isoformat() + "Z"},
    )
    flow.date_action(
        ctx.db,
        ctx.curator,
        p.id,
        p.version,
        {"date_kind": "planned_event", "date": (now() + timedelta(days=60)).isoformat() + "Z"},
    )
    assert not b.work_id and "actual_technical" in b.data["waiting_reason"]
    assert p.dates["planned_technical"] != p.dates["planned_event"]


def test_actual_d0_schedules_remaining_controls_without_backdated_deadlines(ctx):
    route(ctx)
    p = plan(ctx, "technical")
    b = single(ctx, p, "d90")
    p.profile = {**p.profile, "flags": {"startup_cycle": "yes"}}
    flow.date_action(
        ctx.db,
        ctx.curator,
        p.id,
        p.version,
        {
            "date_kind": "actual_technical",
            "date": (now() - timedelta(days=100)).isoformat() + "Z",
            "evidence": "Подтверждённые первые продажи из отчёта",
        },
    )
    w = ctx.db.get(Work, b.work_id)
    assert w and w.original_due_at > now()
    with pytest.raises(BusinessError):
        flow.date_action(
            ctx.db,
            ctx.curator,
            p.id,
            p.version,
            {
                "date_kind": "actual_technical",
                "date": (now() - timedelta(days=1)).isoformat() + "Z",
                "evidence": "Сброс",
            },
        )


def test_red_remark_blocks_opening_yellow_survives_stage_change(ctx):
    p = plan(ctx, "technical")
    result = flow.remark(
        ctx.db,
        ctx.curator,
        p.id,
        p.version,
        {
            "severity": "red",
            "text": "Неисправна электрика",
            "owner_id": "curator",
            "due_at": (now() + timedelta(days=2)).isoformat() + "Z",
        },
    )
    data = {
        "date_kind": "actual_technical",
        "date": (now() - timedelta(minutes=5)).isoformat() + "Z",
        "evidence": "Чек продажи",
    }
    with pytest.raises(BusinessError):
        flow.date_action(ctx.db, ctx.curator, p.id, p.version, data)
    r = ctx.db.get(ProjectRemark, result["id"])
    flow.remark(
        ctx.db,
        ctx.curator,
        p.id,
        p.version,
        {"remark_id": r.id, "remark_version": r.version, "evidence": "Акт устранения"},
    )
    flow.remark(
        ctx.db,
        ctx.curator,
        p.id,
        p.version,
        {
            "severity": "yellow",
            "text": "Доработать оформление",
            "owner_id": "curator",
            "due_at": (now() + timedelta(days=2)).isoformat() + "Z",
        },
    )
    flow.date_action(ctx.db, ctx.curator, p.id, p.version, data)
    assert ctx.db.scalar(select(ProjectRemark).where(ProjectRemark.severity == "yellow")).status == "open"


def launched(ctx, code="support", owner="curator"):
    p = plan(ctx)
    b = single(ctx, p, code)
    route(ctx, CATALOG[code]["route"], owner)
    flow.advance(ctx.db, p)
    return p, b, ctx.db.get(Work, b.work_id)


def test_review_in_reviewer_queue_only_for_named_reviewer(ctx):
    p, b, w = launched(ctx, "advertising_design", "specialist")
    assert w.binding_id == "support" and w.review_binding_id == "marketing"
    assert "marketing_manager" in roles_for(ctx.db, Actor("marketing", "marketing"), w)
    with pytest.raises(AccessDenied):
        roles_for(ctx.db, Actor("other", "marketing"), w)
    transition(ctx.db, ctx.specialist, w.id, w.version, "take", {}, "t")
    transition(ctx.db, ctx.specialist, w.id, w.version, "submit", {"result_description": "Макет приложен"}, "s")
    row = ctx.db.scalar(select(Outbox).where(Outbox.dedupe_key == f"state:{w.id}:{w.version}"))
    assert row.binding_id == "marketing"
    transition(ctx.db, Actor("marketing", "marketing"), w.id, w.version, "accept", {}, "a")
    assert w.outcome == "accepted"


def test_project_cannot_be_cancelled_without_agreement(ctx):
    p, b, w = launched(ctx)
    with pytest.raises(BusinessError):
        transition(ctx.db, ctx.curator, w.id, w.version, "cancel", {"reason": "Снять флажок"}, "c")
    with pytest.raises(BusinessError):
        flow.configure(
            ctx.db,
            ctx.curator,
            p.id,
            p.version,
            {"lane": "curator", "lane_version": 1, "group": "followup", "mode": "not_applicable", "reason": "Нет"},
        )


def test_paid_cancellation_waits_for_fp_and_preserves_ledger(ctx):
    p, b, w = launched(ctx)
    w.data = {**w.data, "paid_started_at": now().isoformat(), "ledger": [{"amount_minor": 10000}]}
    c = flow.request_change(
        ctx.db,
        ctx.curator,
        p.id,
        p.version,
        {
            "block_id": b.id,
            "decision": "cancel",
            "reason": "ФП отказался",
            "settlement": "Возврат отдельно после согласования",
        },
        "c" * 32,
    )
    obj = ctx.db.get(ProjectChange, c["id"])
    flow.approve_change(ctx.db, ctx.curator, obj.id, obj.version, {"approve": True})
    assert w.status != "done" and obj.status == "requested"
    flow.approve_change(ctx.db, ctx.fp, obj.id, obj.version, {"approve": True})
    assert obj.status == "approved" and w.outcome == "cancelled"
    assert w.data["ledger"] == [{"amount_minor": 10000}]


def test_agreement_detects_changed_work_or_revoked_approver(ctx):
    p, b, w = launched(ctx)
    w.data = {**w.data, "paid_started_at": now().isoformat()}
    c = flow.request_change(
        ctx.db,
        ctx.curator,
        p.id,
        p.version,
        {"block_id": b.id, "decision": "cancel", "reason": "Причина", "settlement": "Отдельно"},
        "d" * 32,
    )
    obj = ctx.db.get(ProjectChange, c["id"])
    flow.approve_change(ctx.db, ctx.curator, obj.id, obj.version, {"approve": True})
    role = ctx.db.scalar(select(Role).where(Role.person_id == "curator"))
    role.version += 1
    with pytest.raises(Conflict):
        flow.approve_change(ctx.db, ctx.fp, obj.id, obj.version, {"approve": True})
    assert w.status != "done"


def test_partial_equipment_delivery_never_closes_full_bundle(ctx):
    p, b, w = launched(ctx, "kitchen_equipment")
    item = {
        "key": "oven",
        "model": "Печь",
        "supplier": "Поставщик",
        "quantity": 2,
        "price_minor": 10000,
        "delivery_at": (now() + timedelta(days=10)).isoformat() + "Z",
    }

    def evidence(step, **kw):
        return flow.work_details(ctx.db, ctx.curator, w.id, w.version, {"step": step, "evidence": "Подписано ФП", **kw})

    evidence("specification", items=[item])
    evidence("approve_specification")
    evidence("ordered")
    evidence("receipt", item_key="oven", received_quantity=1)
    with pytest.raises(BusinessError):
        evidence("confirm_receipt")
    evidence("receipt", item_key="oven", received_quantity=2)
    evidence("confirm_receipt")
    assert w.data["procurement_received"] and w.status != "done"
    assert (
        ctx.db.scalar(select(func.count()).select_from(Timer).where(Timer.work_id == w.id, Timer.status == "pending"))
        == 1
    )


def test_smm_preparation_does_not_finish_future_service_period(ctx):
    p, b, w = launched(ctx, "start_smm", "specialist")
    actor = Actor("marketing", "marketing")
    flow.work_details(
        ctx.db,
        actor,
        w.id,
        w.version,
        {
            "step": "smm_period",
            "start_at": now().isoformat() + "Z",
            "until_at": (now() + timedelta(days=30)).isoformat() + "Z",
            "volume": "12 публикаций, материалы от ФП",
            "plan_reference": "План 1",
            "guest_care_owner": "Анна",
            "evidence": "Условия согласованы",
        },
    )
    transition(ctx.db, ctx.specialist, w.id, w.version, "take", {}, "t")
    with pytest.raises(BusinessError, match="будущий период"):
        transition(ctx.db, ctx.specialist, w.id, w.version, "submit", {}, "s")


def test_monthly_campaign_is_internal_deduplicated_and_bounded(ctx):
    route(ctx, "marketing", "marketing", "marketing")
    p = plan(ctx, "existing")
    flow.campaign(
        ctx.db,
        Actor("marketing", "marketing"),
        p.id,
        p.version,
        {
            "title": "Месячный план",
            "start_at": (now() + timedelta(days=1)).isoformat() + "Z",
            "until_at": (now() + timedelta(days=90)).isoformat() + "Z",
            "interval": "monthly",
            "plan_reference": "План 12",
            "control_days": 2,
        },
        "f" * 32,
    )
    c = ctx.db.get(ProjectCampaign, "f" * 32)
    c.next_at = now() - timedelta(minutes=1)
    p.next_at = now() + timedelta(hours=1)
    assert flow.tick(ctx.db)
    works = list(ctx.db.scalars(select(Work).where(Work.origin_key.like("campaign:%"))))
    assert len(works) == 1 and c.next_at > now()
    assert works[0].data["plan_reference"] == "План 12"
    assert all(row.binding_id != "client" for row in ctx.db.scalars(select(Outbox)))
    assert not flow.tick(ctx.db)


def test_d180_acceptance_changes_stage_without_closing_other_work(ctx):
    route(ctx)
    p = plan(ctx, "technical")
    b = single(ctx, p, "d180")
    p.profile = {**p.profile, "flags": {"startup_cycle": "yes"}}
    p.dates = {"actual_technical": (now() - timedelta(days=181)).isoformat() + "Z"}
    flow.advance(ctx.db, p)
    w = ctx.db.get(Work, b.work_id)
    transition(ctx.db, ctx.curator, w.id, w.version, "take", {}, "t")
    transition(ctx.db, ctx.curator, w.id, w.version, "submit", {}, "s")
    transition(ctx.db, ctx.curator, w.id, w.version, "accept", {}, "a")
    assert p.stage == "ongoing" and ctx.w.status != "done"


def test_preview_rolls_back_all_plans_work_timers_and_outbox(ctx):
    route(ctx)
    ctx.db.add(EstablishmentBrand(establishment_id="est", brand_id="brand"))
    ctx.db.flush()
    draft = business.make_draft(
        ctx.db,
        ctx.curator,
        "plan:create",
        "Запуск",
        {"title": "Запуск", "partner_id": "partner", "establishment_id": "est", "brand_id": "brand", "scenario": "new"},
    )
    before = ctx.db.scalar(select(func.count()).select_from(Outbox))
    result = business.preview(ctx.db, ctx.curator, draft)
    assert result["works"] > 0
    assert ctx.db.scalar(select(func.count()).select_from(ProjectPlan)) == 0
    assert ctx.db.scalar(select(func.count()).select_from(Outbox)) == before


@pytest.mark.parametrize("actor_name", ["curator", "fp"])
def test_project_ui_and_client_approval_are_durable(ctx, actor_name):
    p = plan(ctx)
    actor = getattr(ctx, actor_name)
    screen = ui.session(ctx.db, actor)
    response = ui.dispatch(ctx.db, actor, screen, "pp_show", {"project_id": p.id})
    assert "Запуск кафе" in response[0]
    assert all(len(payload.encode()) <= 64 for _, payload in response[1])
    if actor_name == "fp":
        assert "Короткий фильтр" not in str(response)
        with pytest.raises(AccessDenied):
            ui.dispatch(ctx.db, actor, screen, "pp_blocks", {"project_id": p.id})
    else:
        button = next(payload for title, payload in response[1] if "фильтр куратора" in title)
        response = ui.callback(ctx.db, actor, button)
        assert "Выберите блок" in response[0]


def test_fp_can_preview_and_confirm_cancellation_from_project_card(ctx):
    p, b, w = launched(ctx)
    w.data = {**w.data, "paid_started_at": now().isoformat()}
    change = flow.request_change(
        ctx.db,
        ctx.curator,
        p.id,
        p.version,
        {"block_id": b.id, "decision": "cancel", "reason": "Согласованная отмена", "settlement": "Расчёты отдельно"},
        "e" * 32,
    )
    c = ctx.db.get(ProjectChange, change["id"])
    flow.approve_change(ctx.db, ctx.curator, c.id, c.version, {"approve": True})
    draft = business.make_draft(ctx.db, ctx.fp, "plan:approval", "Согласование", {"approve": True}, target=c)
    assert ui.view_draft(ctx.db, ctx.fp, draft.id).id == draft.id
    result = business.preview(ctx.db, ctx.fp, draft)
    assert result["status"] == "approved" and w.status != "done"
    draft.status = "checked"
    draft.before = {"_checked": {"grants": access.grants_hash(ctx.db, ctx.fp)}}
    business.confirm(ctx.db, ctx.fp, draft, draft.version)
    assert w.outcome == "cancelled"
    flow.advance(ctx.db, p)
    assert b.status == "cancelled"


def test_event_plan_shift_does_not_shift_technical_opening(ctx):
    p = plan(ctx)
    b = single(ctx, p, "event")
    route(ctx, "marketing", "marketing", "marketing")
    p.profile = {**p.profile, "flags": {"event": "yes"}}
    technical = (now() + timedelta(days=10)).isoformat() + "Z"
    event = (now() + timedelta(days=30)).isoformat() + "Z"
    flow.date_action(ctx.db, ctx.curator, p.id, p.version, {"date_kind": "planned_technical", "date": technical})
    flow.date_action(ctx.db, ctx.curator, p.id, p.version, {"date_kind": "planned_event", "date": event})
    w = ctx.db.get(Work, b.work_id)
    assert w and w.due_at == flow.parse_date(event)
    assert flow.parse_date(p.dates["planned_technical"]) == flow.parse_date(technical)
    with pytest.raises(BusinessError, match="запущена работа"):
        flow.date_action(
            ctx.db,
            ctx.curator,
            p.id,
            p.version,
            {"date_kind": "planned_event", "date": (now() + timedelta(days=40)).isoformat() + "Z"},
        )


def test_individual_filter_never_changes_neighbor_in_same_group(ctx):
    p = plan(ctx)
    old = flow.blocks(ctx.db, p)["bank"].mode
    flow.configure(
        ctx.db,
        ctx.curator,
        p.id,
        p.version,
        {
            "lane": "curator",
            "lane_version": 1,
            "group": "legal",
            "block_code": "stamp",
            "mode": "not_applicable",
            "reason": "Печать не нужна",
        },
    )
    assert flow.blocks(ctx.db, p)["bank"].mode == old
    assert flow.blocks(ctx.db, p)["stamp"].mode == "not_applicable"


def test_personal_course_cannot_be_inherited_from_document(ctx):
    p = plan(ctx)
    with pytest.raises(BusinessError, match="Личное ознакомление"):
        flow.configure(
            ctx.db,
            ctx.curator,
            p.id,
            p.version,
            {
                "lane": "curator",
                "lane_version": 1,
                "group": "premises",
                "block_code": "fp_training",
                "mode": "inherit",
                "evidence": "Файл чужого обучения",
                "reason": "Нельзя",
            },
        )


def test_mysql_concurrent_projects_share_one_physical_work(ctx):
    if ctx.db.bind.dialect.name != "mysql":
        pytest.skip("Row-lock concurrency requires MySQL")
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    p = plan(ctx)
    ctx.db.add(Entity(id="brand2", kind="brand", name="Второй бренд", data={}))
    ctx.db.flush()
    p2 = plan(ctx, "additional_brand", "b" * 32, "brand2")
    flow.blocks(ctx.db, p2)["company"].mode = "verify"
    route(ctx, "registrations")
    ctx.db.commit()
    barrier = Barrier(2)

    def advance(project_id):
        with ctx.factory.begin() as db:
            target = flow.get(db, project_id, True)
            barrier.wait(timeout=10)
            flow.advance(db, target)
            return flow.blocks(db, target)["company"].work_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(advance, [p.id, p2.id]))
    assert results[0] and results[0] == results[1]
    ctx.db.expire_all()
    assert ctx.db.scalar(select(func.count()).select_from(Work).where(Work.kind == "project.company")) == 1


def test_community_and_start_smm_share_one_work_and_separate_results(ctx):
    p = plan(ctx)
    bs = flow.blocks(ctx.db, p)
    for block in bs.values():
        block.mode = "not_applicable"
    bs["community"].mode = "perform"
    bs["start_smm"].mode = "perform"
    route(ctx, "smm", "specialist")
    flow.advance(ctx.db, p)
    community = bs["community"]
    start = bs["start_smm"]
    assert community.work_id == start.work_id and community.work_id
    w = ctx.db.get(Work, community.work_id)
    actor = Actor("marketing", "marketing")
    flow.work_details(
        ctx.db,
        actor,
        w.id,
        w.version,
        {
            "step": "community_ready",
            "community_url": "https://vk.com/ama_test",
            "admin_access": "Права проверены по приглашению администратора",
            "evidence": "Ссылка, оформление, наполнение и доступ проверены",
        },
    )
    flow.work_details(
        ctx.db,
        actor,
        w.id,
        w.version,
        {
            "step": "smm_period",
            "start_at": now().isoformat() + "Z",
            "until_at": (now() + timedelta(days=30)).isoformat() + "Z",
            "volume": "12 публикаций, материалы ФП по плану",
            "plan_reference": "План №7",
            "guest_care_owner": "Анна",
            "evidence": "Период и объём согласованы",
        },
    )
    assert flow.ready(ctx.db, community)
    assert not flow.ready(ctx.db, start)
    assert flow.prerequisite_ready(ctx.db, start, "readiness")
    assert ctx.db.scalar(select(func.count()).select_from(Work).where(Work.project_id == p.id)) == 1


def test_readiness_cannot_bypass_personal_training_or_guest_care(ctx):
    p, b, w = launched(ctx, "readiness")
    transition(ctx.db, ctx.curator, w.id, w.version, "take", {}, "t")
    with pytest.raises(BusinessError, match="Обязательное условие"):
        transition(ctx.db, ctx.curator, w.id, w.version, "submit", {}, "s")


def test_expired_campaign_does_not_create_backdated_work(ctx):
    p = plan(ctx, "existing")
    p.next_at = now() + timedelta(hours=1)
    ctx.db.add(
        ProjectCampaign(
            id="0" * 32,
            project_id=p.id,
            title="Прошлая акция",
            owner_id="marketing",
            next_at=now() - timedelta(days=32),
            until_at=now() - timedelta(days=1),
            data={
                "interval": "monthly",
                "start_at": (now() - timedelta(days=32)).isoformat() + "Z",
                "plan_reference": "Старый план",
                "day": 1,
            },
        )
    )
    ctx.db.flush()
    assert flow.tick(ctx.db)
    assert not ctx.db.get(ProjectCampaign, "0" * 32).enabled
    assert not ctx.db.scalar(select(Work.id).where(Work.origin_key.like("campaign:%")))


def test_executor_gets_agreement_notification_and_can_find_it(ctx):
    p, b, w = launched(ctx, "advertising_design", "specialist")
    c = flow.request_change(
        ctx.db,
        Actor("marketing", "marketing"),
        p.id,
        p.version,
        {
            "block_id": b.id,
            "decision": "cancel",
            "reason": "Изменился план",
            "settlement": "Нет финансовых обязательств",
        },
        "1" * 32,
    )
    from ama.ui.projects import visible

    assert visible(ctx.db, ctx.specialist, p)
    screen = ui.session(ctx.db, ctx.specialist)
    text, buttons = ui.dispatch(ctx.db, ctx.specialist, screen, "pp_show", {"project_id": p.id})
    assert "Изменился план" in text
    assert any("Согласование" in title for title, _ in buttons)
    assert ctx.db.scalar(select(Outbox).where(Outbox.dedupe_key == f"project-change:{c['id']}:specialist"))
