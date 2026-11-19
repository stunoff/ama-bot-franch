from datetime import timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import select, func
from ama import digital as d
from ama.auth import Actor
from ama.errors import AccessDenied, BusinessError, Conflict
from ama.models import (
    DigitalProgram,
    DigitalFact,
    DigitalStep,
    EstablishmentBrand,
    Entity,
    Binding,
    Record,
    Work,
    Training,
    now,
)
from ama.service import transition
from ama.ui import business, engine, controller, schema


@pytest.fixture
def dg(ctx):
    db = ctx.db
    db.add(EstablishmentBrand(establishment_id="est", brand_id="brand"))
    db.add_all(
        [
            Binding(id="software", platform="telegram", chat_id="-900", purpose="software", active=True, verified=True),
            Binding(id="reviews", platform="telegram", chat_id="-901", purpose="reviews", active=True, verified=True),
        ]
    )
    r = db.scalar(select(Record).where(Record.kind == "route", Record.key == "support"))
    r.data = {**r.data, "owner_id": "specialist"}
    for purpose in ["software", "reviews"]:
        db.add(
            Record(
                kind="route",
                key=purpose,
                published=True,
                data={"binding_id": purpose, "owner_id": "specialist", "reviewer_id": "curator", "calendar": "support"},
            )
        )
    db.flush()
    info = {
        "establishment_id": "est",
        "primary_binding_id": "client",
        "partner_person_id": "fp",
        "calendar": "support",
        "due_at": (now() + timedelta(days=12)).isoformat() + "Z",
    }
    ident = d.start(db, ctx.curator, info, "dg-start")["id"]
    p = d.get(db, ident)
    return SimpleNamespace(db=db, p=p, ctx=ctx, info=info)


def step(s, key, scope=None):
    return s.db.scalar(
        select(DigitalStep).where(
            DigitalStep.program_id == s.p.id,
            DigitalStep.component == key,
            DigitalStep.scope_key == ("brand" if d.COMPONENTS[key][5] else "common")
            if scope is None
            else DigitalStep.scope_key == scope,
        )
    )


def answer(s, key, v, scope=None, actor=None):
    return d.save_fact(
        s.db,
        actor or s.ctx.fp,
        s.p.id,
        s.p.version,
        {"field": key, "scope": scope or ("brand" if d.LOOKUP[key]["brand"] else "common"), "value": v},
    )


def config(s, key, mode="connect", **kw):
    z = step(s, key)
    return d.configure(
        s.db,
        s.ctx.curator,
        s.p.id,
        s.p.version,
        {
            "step_id": z.id,
            "mode": mode,
            "reason": "Подтверждённая конфигурация",
            "due_at": (now() + timedelta(days=12)).isoformat() + "Z",
            **kw,
        },
    )


def configure_all(s):
    for z in s.db.scalars(select(DigitalStep).where(DigitalStep.program_id == s.p.id)):
        config(
            s,
            z.component,
            "connect"
            if z.component in {"equipment", "iiko", "care", "ama_rs", "telephony", "sellkit", "zones"}
            else "na",
        )


def accepted(s, key):
    # Fixture facts for independent downstream-result tests, not a production acceptance shortcut.
    z = step(s, key)
    z.status = "accepted"
    z.mode = "connect"


def seed_facts(s, component):
    z = step(s, component)
    values = {
        "phone": "+79991234567",
        "phone_owner": "fp",
        "phone_ready": True,
        "same_phone": True,
        "internet": "ready",
        "equipment": [{"name": "Касса", "quantity": 1, "state": "ready", "specification": "https://example.org/spec"}],
        "equipment_approved": True,
        "stations": "https://example.org/stations",
        "menu_mode": "base",
        "menu_excluded": [],
        "menu_added": [],
        "menu_apply": now().isoformat() + "Z",
        "price_mode": "base",
        "methods": ["pickup"],
        "pickup_terms": "Утверждённые базовые условия",
        "own_delivery": False,
        "payments": ["cash"],
        "bank": "Базовые банковские вводные",
        "call_center": False,
        "call_scheme": "Согласовано",
        "work_phone_messenger": "max",
        "management_messenger": "telegram",
        "account_ready": True,
        "initial_contact": "ФП контакт",
        "operational_chat": "Создать",
        "management_chat": "Использовать действующий",
        "participants": [{"name": "ФП", "roles": ["фп", "управляющий"], "contact": "@fp"}],
        "guest_links": [{"name": "Вход", "url": "https://example.org/guest"}],
        "care_sources": ["Гостевой вход"],
        "care_calendar": "support",
        "care_escalation": "Не предусмотрен",
        "sellkit_local": "Базовые",
    }
    for k in d.fields_for_step(s.db, s.p, z):
        spec = d.LOOKUP[k]
        sc = "brand" if spec["brand"] else "common"
        if not d.fact(s.db, s.p, k, sc):
            s.db.add(
                DigitalFact(
                    program_id=s.p.id,
                    scope_key=sc,
                    field=k,
                    data={
                        "state": "known",
                        "value": values.get(k, "Тестовые утверждённые данные"),
                        "confirmed": True,
                        "source": "fixture",
                    },
                )
            )
    s.db.flush()


def payload(s, key):
    return {
        "step_id": step(s, key).id,
        "evidence": "Проверено по регламенту",
        "actual_configuration": "Фактические ID и настройки проверены",
        "tested_at": (now() - timedelta(minutes=1)).isoformat() + "Z",
        "external_ids": "ID предприятия 123; ссылка подтверждена",
        "published_at": (now() - timedelta(minutes=1)).isoformat() + "Z",
        "tests": {k: {"passed": True, "evidence": "Фактический тест " + k} for k in d.COMPONENTS[key][4]},
    }


def activate(s):
    configure_all(s)
    return d.activate(s.db, s.ctx.curator, s.p.id, s.p.version, {})


def executor(s, key):
    return Actor("specialist", s.db.get(Work, step(s, key).work_id).binding_id)


def test_one_physical_passport_no_duplicate_on_repeated_start(dg):
    s = dg
    assert d.start(s.db, s.ctx.curator, s.info, "other")["id"] == s.p.id
    assert s.db.scalar(select(func.count()).select_from(DigitalProgram)) == 1


def test_staff_import_does_not_fake_partner_confirmation(dg):
    s = dg
    answer(s, "work_phone_messenger", "max", actor=s.ctx.curator)
    assert d.value(s.db, s.p, "work_phone_messenger") is None
    answer(s, "work_phone_messenger", "max")
    assert d.value(s.db, s.p, "work_phone_messenger") == "max"
    assert d.value(s.db, s.p, "management_messenger") is None


@pytest.mark.parametrize(
    "work,management", [("max", "max"), ("telegram", "telegram"), ("max", "telegram"), ("telegram", "max")]
)
def test_four_messenger_combinations_do_not_move_client_chat(dg, work, management):
    s = dg
    answer(s, "work_phone_messenger", work)
    answer(s, "management_messenger", management)
    assert (
        d.value(s.db, s.p, "work_phone_messenger") == work and d.value(s.db, s.p, "management_messenger") == management
    )
    assert s.db.get(Binding, "client").platform == "telegram"


def test_observer_and_technical_admin_have_no_fp_decision_authority(dg):
    for actor in [dg.ctx.observer, dg.ctx.admin]:
        with pytest.raises(AccessDenied):
            answer(dg, "call_center", False, actor=actor)


def test_no_delivery_skips_zones_and_chibis_conflict_is_visible(dg):
    s = dg
    answer(s, "own_delivery", False)
    assert not d.allowed_field(s.db, s.p, "zones", "brand")
    assert "conflict:chibis_without_own_delivery" in d.missing(s.db, s.p, step(s, "agg_chibis"))
    assert d.allowed_field(s.db, s.p, "pickup_terms", "brand")


def test_individual_price_requires_document_but_independent_contacts_continue(dg):
    s = dg
    answer(s, "price_mode", "individual")
    assert "price_reference" in d.missing(s.db, s.p, step(s, "iiko"))
    answer(s, "initial_contact", "@work_phone")
    assert "price_reference" not in d.missing(s.db, s.p, step(s, "ama_rs"))


def test_zones_need_fp_commercial_confirmation_and_distinct_maps(dg):
    s = dg
    answer(s, "own_delivery", True)
    zone = {
        "name": "Центр",
        "map_url": "https://yandex.ru/maps/?um=constructor.zone1",
        "minimum_minor": 0,
        "delivery_minor": 0,
        "free_minor": 100000,
        "minutes": 30,
        "hours": "11–20",
    }
    with pytest.raises(BusinessError, match="коммерческие"):
        answer(s, "zones", [zone])
    answer(s, "delivery_terms", "Условия подтверждены ФП")
    with pytest.raises(BusinessError, match="отдельной"):
        answer(s, "zones", [zone, {**zone, "name": "Вторая"}])
    answer(s, "zones", [zone])
    assert d.value(s.db, s.p, "zones", "brand")[0]["name"] == "Центр"


def test_seating_false_does_not_hide_general_qr(dg):
    answer(dg, "seating", False)
    assert not d.allowed_field(dg.db, dg.p, "tables", "common")
    assert d.allowed_field(dg.db, dg.p, "general_qr", "common")


def test_gap_and_help_persist_control_and_one_question(dg):
    s = dg
    data = {
        "field": "phone",
        "scope": "common",
        "state": "help",
        "reason": "Нужна помощь с SIM",
        "responsible_id": "fp",
        "next_check": (now() + timedelta(days=1)).isoformat() + "Z",
    }
    for _ in range(2):
        d.save_fact(s.db, s.ctx.fp, s.p.id, s.p.version, data)
    assert d.fact(s.db, s.p, "phone").data["state"] == "help"
    assert s.db.scalar(select(func.count()).select_from(Work).where(Work.kind == "digital_question")) == 1
    assert d.value(s.db, s.p, "phone") is None


def test_configuration_is_explicit_and_care_required_without_cc(dg):
    answer(dg, "call_center", False)
    with pytest.raises(BusinessError, match="применимость"):
        d.plan_snapshot(dg.db, dg.p)
    with pytest.raises(BusinessError, match="Обязательную"):
        config(dg, "care", "na")


def test_plan_groups_fields_into_executor_topics_and_idempotent(dg):
    s = dg
    activate(s)
    groups = list(s.db.scalars(select(Work).where(Work.kind == "digital_group")))
    assert len(groups) == 4  # software common/brand, support common/brand
    before = len(groups)
    d.activate(s.db, s.ctx.curator, s.p.id, s.p.version, {})
    assert s.db.scalar(select(func.count()).select_from(Work).where(Work.kind == "digital_group")) == before
    software = next(w for w in groups if w.brand_id == "brand" and w.data["digital_route"] == "software")
    assert step(s, "iiko").work_id == step(s, "sellkit").work_id == software.id


def test_executor_subpassport_excludes_other_directions(dg):
    s = dg
    seed_facts(s, "ama_rs")
    seed_facts(s, "iiko")
    packet = d.task_payload(s.db, s.p, step(s, "iiko"))
    assert "price_mode" in packet["facts"] and "management_messenger" not in packet["facts"]
    packet = d.task_payload(s.db, s.p, step(s, "ama_rs"))
    assert "management_messenger" in packet["facts"] and "price_mode" not in packet["facts"]


def test_hardware_partial_does_not_block_contact_preparation_but_blocks_test(dg):
    s = dg
    seed_facts(s, "equipment")
    f = d.fact(s.db, s.p, "equipment")
    f.data = {**f.data, "value": [{**f.data["value"][0], "state": "partial"}]}
    assert "hardware_or_network_not_ready" in d.missing(s.db, s.p, step(s, "equipment"))
    assert "hardware_or_network_not_ready" not in d.missing(s.db, s.p, step(s, "ama_rs"))


def test_external_bots_need_both_directions_and_separate_acceptance(dg):
    s = dg
    activate(s)
    seed_facts(s, "ama_rs")
    seed_facts(s, "care")
    data = payload(s, "ama_rs")
    data["tests"]["management"]["passed"] = False
    with pytest.raises(BusinessError, match="контрольный"):
        d.result(s.db, executor(s, "ama_rs"), s.p.id, s.p.version, data)
    data["tests"]["management"]["passed"] = True
    d.result(s.db, executor(s, "ama_rs"), s.p.id, s.p.version, data)
    assert step(s, "ama_rs").status == "review" and step(s, "care").status != "accepted"
    d.review(s.db, s.ctx.curator, s.p.id, s.p.version, {"step_id": step(s, "ama_rs").id, "accept": True})
    assert step(s, "ama_rs").status == "accepted" and step(s, "care").status != "accepted"


def test_price_change_reopens_only_brand_related_results_and_keeps_history(dg):
    s = dg
    answer(s, "price_mode", "base")
    for k in ["iiko", "sellkit", "agg_yandex", "telephony", "ama_rs", "care", "geo"]:
        accepted(s, k)
    answer(s, "price_mode", "individual")
    assert step(s, "iiko").status == step(s, "sellkit").status == "rework"
    assert step(s, "ama_rs").status == step(s, "telephony").status == step(s, "care").status == "accepted"
    assert step(s, "iiko").data["history"][-1]["event"] == "invalidated"


def test_zones_change_does_not_reset_platform_delivery_aggregator(dg):
    s = dg
    answer(s, "own_delivery", True)
    answer(s, "delivery_terms", "Подтверждены")
    answer(s, "yandex_delivery", "platform")
    for k in ["agg_yandex", "agg_chibis", "sellkit", "ama_rs"]:
        accepted(s, k)
    answer(
        s,
        "zones",
        [
            {
                "name": "Одна",
                "map_url": "https://yandex.ru/maps/?um=constructor.a",
                "minimum_minor": 1,
                "delivery_minor": 0,
                "free_minor": 100,
                "minutes": 30,
                "hours": "10–20",
            }
        ],
    )
    assert step(s, "agg_yandex").status == "accepted" and step(s, "agg_chibis").status == "rework"
    assert step(s, "sellkit").status == "rework" and step(s, "ama_rs").status == "accepted"


def test_no_bulk_work_accept_or_legacy_tech_confirm_bypass(dg):
    s = dg
    activate(s)
    w = s.db.get(Work, step(s, "ama_rs").work_id)
    with pytest.raises(BusinessError, match="мастере"):
        transition(s.db, executor(s, "ama_rs"), w.id, w.version, "submit", {}, "bypass")
    from ama.operations import operation

    root = d.root(s.db, s.p)
    with pytest.raises(BusinessError, match="мастер"):
        operation(s.db, s.ctx.curator, root.id, root.version, "passport", {"systems": ["iiko"]})


def test_second_brand_preserves_common_facts_and_proofs(dg):
    s = dg
    answer(s, "phone", "+79991234567")
    accepted(s, "ama_rs")
    s.db.add(Entity(id="brand2", kind="brand", name="Второй", data={}))
    s.db.flush()
    s.db.add(EstablishmentBrand(establishment_id="est", brand_id="brand2"))
    s.db.flush()
    d.add_brand(
        s.db,
        s.ctx.curator,
        s.p.id,
        s.p.version,
        {
            "brand_id": "brand2",
            "reason": "Дополнительный бренд",
            "due_at": (now() + timedelta(days=10)).isoformat() + "Z",
        },
    )
    assert d.value(s.db, s.p, "phone") == "+79991234567" and step(s, "ama_rs").status == "accepted"
    assert s.db.scalar(select(func.count()).select_from(DigitalStep).where(DigitalStep.component == "ama_rs")) == 1
    assert s.db.scalar(select(func.count()).select_from(DigitalStep).where(DigitalStep.component == "care")) == 2


def test_stale_form_and_wrong_enterprise_cannot_change_facts(dg):
    s = dg
    old = s.p.version
    answer(s, "call_center", False)
    with pytest.raises(Conflict):
        d.save_fact(s.db, s.ctx.fp, s.p.id, old, {"field": "phone", "value": "+79991234567"})
    s.db.add(Entity(id="outside-est", kind="establishment", name="Другой адрес", partner_id="partner", data={}))
    s.db.flush()
    s.db.get(Binding, "client").establishment_id = "outside-est"
    with pytest.raises(AccessDenied):
        answer(s, "phone", "+79991234567")


def test_secret_fields_are_rejected_and_phone_not_personal_prefill(dg):
    assert d.value(dg.db, dg.p, "phone") is None
    with pytest.raises(BusinessError, match="Секреты"):
        answer(dg, "initial_contact", "token=secret")


def test_named_course_is_personal_and_does_not_accept_telephony(dg):
    s = dg
    s.db.add(
        Record(
            kind="course",
            key="call_center",
            published=True,
            data={"cards": [{"title": "КЦ", "text": "Утверждённый материал"}]},
        )
    )
    s.db.flush()
    d.call_center_course(s.db, s.ctx.curator, s.p.id, s.p.version, {"person_id": "fp", "course_key": "call_center"})
    assert s.db.scalar(select(Training).where(Training.person_id == "fp", Training.course_key == "call_center"))
    assert step(s, "telephony").status != "accepted"


def test_persistent_ui_and_preview_have_no_tasks_before_confirmation(dg):
    s = dg
    screen = engine.session(s.db, s.ctx.curator)
    text, buttons = controller.dispatch(s.db, s.ctx.curator, screen, "dg_view", {"id": s.p.id})
    assert "Цифровое" in text and all(len(b[1].encode()) <= 64 for b in buttons)
    draft = business.make_draft(
        s.db,
        s.ctx.fp,
        "digital:fact:call_center",
        "КЦ",
        {"field": "call_center", "scope": "common", "value": False},
        s.p,
    )
    before = s.p.version
    result = business.preview(s.db, s.ctx.fp, draft)
    assert result["field"] == "call_center" and d.fact(s.db, s.p, "call_center") is None and s.p.version == before
    assert schema.fields_for(draft.kind)[0]["type"] == "bool"


def test_plan_preview_rollback_and_route_change_recheck(dg):
    s = dg
    configure_all(s)
    draft = business.make_draft(s.db, s.ctx.curator, "digital:activate", "План", {}, s.p)
    result = business.preview(s.db, s.ctx.curator, draft)
    assert result["steps"] > 0 and not list(s.db.scalars(select(Work).where(Work.kind == "digital_group")))
    draft.status = "checked"
    draft.data = {**draft.data, "_checked": {"digital_fingerprint": result["fingerprint"]}}
    # Use domain fingerprint assertion independently of UI checked metadata serialization.
    route = s.db.scalar(select(Record).where(Record.kind == "route", Record.key == "software"))
    route.version += 1
    with pytest.raises(Conflict):
        d.activate(s.db, s.ctx.curator, s.p.id, s.p.version, {"_fingerprint": result["fingerprint"]})


def test_cannot_claim_final_readiness_by_sending_requests(dg):
    activate(dg)
    with pytest.raises(BusinessError, match="непринятые"):
        d.complete(dg.db, dg.ctx.curator, dg.p.id, dg.p.version, {"evidence": "Отправили заявки"})


def test_primary_chat_picker_contains_client_binding(dg):
    from ama.ui import repository

    draft = business.make_draft(dg.db, dg.ctx.curator, "digital:start", "Запуск", dg.info)
    assert ("client", repository.label(dg.db, dg.db.get(Binding, "client"))) in repository.refs(
        dg.db, dg.ctx.curator, "digital_client", draft
    )


def test_scoped_request_uses_confirmed_delivery_sla(dg):
    from ama import sla

    s = dg
    result = d.request(
        s.db,
        s.ctx.curator,
        s.p.id,
        s.p.version,
        {
            "field": "phone",
            "scope": "common",
            "reason": "Нужен для рабочего аккаунта",
            "next_check": (now() + timedelta(days=2)).isoformat() + "Z",
        },
    )
    c = sla.case_for(s.db, s.db.get(Work, result["work_id"]))
    assert c.data["request"]["delivered_at"] is None
    assert d.value(s.db, s.p, "phone") is None


def test_master_address_change_rechecks_only_related_results_once(dg):
    s = dg
    activate(s)
    for k in ["ama_rs", "iiko", "equipment", "screens"]:
        accepted(s, k)
    s.db.get(Entity, "est").data = {"address": "Новый подтверждённый адрес"}
    assert d.tick(s.db)
    assert step(s, "ama_rs").status == step(s, "iiko").status == "rework"
    assert step(s, "equipment").status == "accepted"
    assert not d.tick(s.db)


def test_one_blocker_signal_for_common_missing_phone(dg):
    from ama.models import SLASignal

    s = dg
    activate(s)
    for z in s.db.scalars(select(DigitalStep).where(DigitalStep.program_id == s.p.id)):
        z.due_at = now() - timedelta(minutes=1)
    d.tick(s.db)
    count = s.db.scalar(select(func.count()).select_from(SLASignal))
    assert count > 0
    d.tick(s.db)
    assert s.db.scalar(select(func.count()).select_from(SLASignal)) == count
    assert (
        s.db.scalar(
            select(func.count())
            .select_from(SLASignal)
            .where(SLASignal.data["reason"].as_string() == "Не принят цифровой результат: phone")
        )
        == 1
    )


def test_external_result_with_secret_or_missing_source_test_is_rejected(dg):
    s = dg
    activate(s)
    seed_facts(s, "care")
    data = payload(s, "care")
    with pytest.raises(BusinessError, match="источника"):
        d.result(s.db, executor(s, "care"), s.p.id, s.p.version, data)
    data["source_results"] = {"Гостевой вход": {"passed": True, "evidence": "Проверено"}}
    data["actual_configuration"] = "password=secret"
    with pytest.raises(BusinessError, match="Секреты"):
        d.result(s.db, executor(s, "care"), s.p.id, s.p.version, data)


def test_named_curator_cannot_accept_own_specialist_result(dg):
    s = dg
    activate(s)
    z = step(s, "ama_rs")
    z.status = "review"
    w = s.db.get(Work, z.work_id)
    w.executor_id = "curator"
    with pytest.raises(AccessDenied, match="собственную"):
        d.review(s.db, s.ctx.curator, s.p.id, s.p.version, {"step_id": z.id, "accept": True})


def test_rework_and_original_group_deadline_are_preserved(dg):
    s = dg
    activate(s)
    seed_facts(s, "ama_rs")
    d.result(s.db, executor(s, "ama_rs"), s.p.id, s.p.version, payload(s, "ama_rs"))
    w = s.db.get(Work, step(s, "ama_rs").work_id)
    original = w.original_due_at
    d.review(
        s.db,
        s.ctx.curator,
        s.p.id,
        s.p.version,
        {
            "step_id": step(s, "ama_rs").id,
            "accept": False,
            "reason": "Уточнить маршрут управления",
            "due_at": (now() + timedelta(days=15)).isoformat() + "Z",
        },
    )
    assert step(s, "ama_rs").status == "rework" and w.original_due_at == original
    assert step(s, "ama_rs").data["history"][-1]["reason"] == "Уточнить маршрут управления"


def test_two_enterprises_in_same_chat_keep_answers_separate(dg):
    s = dg
    s.db.add(Entity(id="est2", kind="establishment", partner_id="partner", name="Второе кафе", data={}))
    s.db.flush()
    s.db.add(EstablishmentBrand(establishment_id="est2", brand_id="brand"))
    s.db.flush()
    other = d.get(s.db, d.start(s.db, s.ctx.curator, {**s.info, "establishment_id": "est2"}, "second-est")["id"])
    answer(s, "phone", "+79991234567")
    assert d.value(s.db, other, "phone") is None


def test_concurrent_fp_answers_have_one_winner(dg):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    if dg.db.bind.dialect.name != "mysql":
        pytest.skip("Requires MySQL row locks")
    s = dg
    s.db.commit()
    ident, version = s.p.id, s.p.version
    barrier = Barrier(2)

    def run(number):
        with s.ctx.factory.begin() as db:
            barrier.wait()
            try:
                d.save_fact(db, s.ctx.fp, ident, version, {"field": "phone", "scope": "common", "value": number})
                return "ok"
            except Conflict:
                return "conflict"

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(run, ["+79991111111", "+79992222222"]))
    assert sorted(results) == ["conflict", "ok"]
    s.db.expire_all()
    assert d.get(s.db, ident).version == version + 1


@pytest.mark.parametrize("cc", [False, True])
def test_complete_readiness_requires_independent_tests_and_personal_cc_course(dg, cc):
    s = dg
    required = {"equipment", "payment_cash", "iiko", "ama_rs", "care"}
    for z in s.db.scalars(select(DigitalStep).where(DigitalStep.program_id == s.p.id)):
        config(s, z.component, "connect" if z.component in required else "na")
    d.activate(s.db, s.ctx.curator, s.p.id, s.p.version, {})
    for key in ["equipment", "payment_cash", "iiko", "ama_rs", "care"]:
        seed_facts(s, key)
        if key == "ama_rs":
            answer(s, "call_center", cc)
        data = payload(s, key)
        if key == "care":
            data["source_results"] = {"Гостевой вход": {"passed": True, "evidence": "Тест нужного бренда успешен"}}
        d.result(s.db, executor(s, key), s.p.id, s.p.version, data)
        d.review(s.db, s.ctx.curator, s.p.id, s.p.version, {"step_id": step(s, key).id, "accept": True})
    if cc:
        with pytest.raises(BusinessError, match="курс"):
            d.complete(s.db, s.ctx.curator, s.p.id, s.p.version, {"evidence": "Финальная проверка"})
        s.db.add(
            Record(
                kind="course",
                key="call_center",
                published=True,
                data={"cards": [{"title": "КЦ", "text": "Утверждённый курс", "required_materials": []}]},
            )
        )
        s.db.flush()
        d.call_center_course(s.db, s.ctx.curator, s.p.id, s.p.version, {"person_id": "fp", "course_key": "call_center"})
        t = s.db.scalar(select(Training).where(Training.course_key == "call_center"))
        t.completed_at = now()
    d.complete(s.db, s.ctx.curator, s.p.id, s.p.version, {"evidence": "Все применимые проверки подтверждены людьми"})
    assert s.p.data["ready"] and d.root(s.db, s.p).outcome == "accepted"


def test_real_ui_preview_confirm_continues_program(dg):
    s = dg
    actor = s.ctx.fp
    screen = engine.session(s.db, actor)
    draft = business.make_draft(
        s.db, actor, "digital:fact:call_center", "КЦ", {"field": "call_center", "scope": "common", "value": False}, s.p
    )
    controller.dispatch(s.db, actor, screen, "preview", {"draft_id": draft.id})
    text, buttons = controller.dispatch(
        s.db, actor, screen, "confirm", {"draft_id": draft.id, "expected": draft.version}
    )
    assert "Подтверждено" in text and d.value(s.db, s.p, "call_center") is False
    assert any("Продолжить цифровое" in b[0] for b in buttons)


def test_prepared_vitrine_cannot_be_accepted_without_iiko_order(dg):
    s = dg
    activate(s)
    seed_facts(s, "sellkit")
    accepted(s, "equipment")
    accepted(s, "zones")
    accepted(s, "payment_cash")
    assert "dependency:iiko" in d.missing(s.db, s.p, step(s, "sellkit"))
    with pytest.raises(BusinessError, match="iiko"):
        d.result(s.db, executor(s, "sellkit"), s.p.id, s.p.version, payload(s, "sellkit"))


def test_unverified_calendar_and_generic_yandex_map_are_not_configuration(dg):
    s = dg
    with pytest.raises(BusinessError, match="календарь"):
        answer(s, "care_calendar", "missing")
    answer(s, "own_delivery", True)
    answer(s, "delivery_terms", "Согласовано ФП")
    with pytest.raises(BusinessError, match="отдельной"):
        answer(
            s,
            "zones",
            [
                {
                    "name": "Общая карта",
                    "map_url": "https://yandex.ru/maps/",
                    "minimum_minor": 0,
                    "delivery_minor": 0,
                    "free_minor": 1,
                    "minutes": 30,
                    "hours": "11–20",
                }
            ],
        )


def test_object_cc_course_works_without_partner_wide_flag(dg):
    from ama.service import training_action

    s = dg
    answer(s, "call_center", True)
    s.db.add(
        Record(
            kind="course",
            key="call_center",
            published=True,
            data={"cards": [{"title": "КЦ", "text": "Утверждённый курс", "required_materials": []}]},
        )
    )
    s.db.flush()
    assert not s.db.get(Entity, "partner").data.get("call_center_applicable")
    d.call_center_course(s.db, s.ctx.curator, s.p.id, s.p.version, {"person_id": "fp", "course_key": "call_center"})
    t = training_action(s.db, s.ctx.fp, "call_center", "open")
    training_action(s.db, s.ctx.fp, "call_center", "next", t.version)
    assert t.completed_at is not None


def test_personal_course_does_not_grant_others_or_unrelated_chat_access(dg):
    s = dg
    answer(s, "call_center", True)
    s.p.data = {**s.p.data, "course_assignment": {"person_id": "fp", "course_key": "call_center", "course_version": 1}}
    assert d.course_access(s.db, s.ctx.fp, "call_center")
    assert not d.course_access(s.db, s.ctx.observer, "call_center")
    assert not d.course_access(s.db, Actor("fp", "marketing"), "call_center")
    answer(s, "call_center", False)
    assert not d.course_access(s.db, s.ctx.fp, "call_center")


def test_approved_price_file_only_to_assigned_executor_and_rechecked(dg):
    from ama.models import Attachment

    s = dg
    activate(s)
    answer(s, "price_mode", "individual")
    asset = Attachment(
        work_id=s.p.work_id,
        author_id="fp",
        platform="telegram",
        file_id="file-id",
        filename="prices.pdf",
        source_key="approved-price",
        visibility="client",
        status="stored",
        storage_path="/tmp/approved-price.pdf",
        sha256="a" * 64,
        size=10,
    )
    s.db.add(asset)
    s.db.flush()
    answer(s, "price_reference", "attachment:" + asset.id)
    row = d.send_file(s.db, executor(s, "iiko"), s.p.id, step(s, "iiko").id, "price_reference")
    assert row.payload["attachment_id"] == asset.id and d.deliverable(s.db, row)
    with pytest.raises(AccessDenied):
        d.send_file(s.db, s.ctx.fp, s.p.id, step(s, "iiko").id, "price_reference")
    answer(s, "price_reference", "https://example.org/new-approved-prices")
    assert not d.deliverable(s.db, row)
