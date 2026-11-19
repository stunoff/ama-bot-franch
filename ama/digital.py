"""Human-confirmed digital onboarding; one physical passport, independent results."""

from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs
import json
import re
from sqlalchemy import select
from ama.config import settings
from ama.models import (
    ProjectPlan,
    Attachment,
    DigitalProgram,
    DigitalFact,
    DigitalStep,
    Work,
    Entity,
    Binding,
    EstablishmentBrand,
    Record,
    Training,
    Outbox,
    now,
)
from ama.auth import Actor, active_person_role
from ama.errors import BusinessError, AccessDenied, Conflict
from ama.service import versioned, parse_date, queue, create_work
from ama.digital_schema import LOOKUP, COMPONENTS


def root(db, p):
    return db.get(Work, p.work_id)


def get(db, ident, lock=False):
    q = select(DigitalProgram).where(DigitalProgram.id == ident, DigitalProgram.bot_id == settings().bot_id)
    p = db.scalar(q.with_for_update().execution_options(populate_existing=True) if lock else q)
    if not p:
        raise AccessDenied("Цифровой паспорт недоступен.")
    return p


def brands(db, p):
    return [
        x
        for x in p.data["brands"]
        if db.scalar(
            select(EstablishmentBrand.id).where(
                EstablishmentBrand.establishment_id == p.establishment_id,
                EstablishmentBrand.brand_id == x,
                EstablishmentBrand.active.is_(True),
            )
        )
    ]


def authorize(db, actor, p, *, curator=False, write=False, scope=None):
    w = root(db, p)
    b = db.get(Binding, actor.binding_id)
    if not b or not b.active or not b.verified or b.bot_id != p.bot_id:
        raise AccessDenied("Нужна действующая проверенная привязка.")
    if b.purpose == "client":
        if (
            curator
            or b.partner_id != w.partner_id
            or b.brand_id not in brands(db, p)
            or b.establishment_id not in {None, p.establishment_id}
        ):
            raise AccessDenied("Паспорт другого предприятия / бренда.")
        if scope and scope != "common" and scope != b.brand_id:
            raise AccessDenied("Данные другого бренда.")
        allowed = {"partner"} if write else {"partner", "observer"}
        area = SimpleNamespace(partner_id=w.partner_id, establishment_id=p.establishment_id, brand_id=b.brand_id)
        if (
            not active_person_role(db, actor.person_id, allowed, area)
            or write
            and actor.person_id != w.partner_person_id
        ):
            raise AccessDenied("Решение подтверждает только назначенный ФП.")
        return "partner" if write else "client"
    if (
        actor.person_id == w.reviewer_id
        and active_person_role(
            db,
            actor.person_id,
            {"curator"},
            SimpleNamespace(partner_id=w.partner_id, establishment_id=p.establishment_id, brand_id=None),
        )
        and b.purpose == "support"
        and (not b.partner_id or b.partner_id == w.partner_id)
        and (not b.establishment_id or b.establishment_id == p.establishment_id)
    ):
        if b.brand_id and (scope in {None, "common"} or b.brand_id != scope):
            raise AccessDenied("Для общего паспорта нужен маршрут куратора всех брендов.")
        return "curator"
    if curator:
        raise AccessDenied("Действие назначенного куратора в сопровождении.")
    steps = list(
        db.scalars(select(DigitalStep).where(DigitalStep.program_id == p.id, DigitalStep.work_id.is_not(None)))
    )
    for s in steps:
        task = db.get(Work, s.work_id)
        if (
            task
            and task.executor_id == actor.person_id
            and task.binding_id == actor.binding_id
            and active_person_role(db, actor.person_id, {"specialist"}, task)
            and (scope is None or scope == "common" or s.scope_key == scope)
        ):
            return "specialist"
    raise AccessDenied("Нет назначения в этой очереди цифрового подключения.")


def fact(db, p, key, scope="common"):
    if not LOOKUP[key]["brand"]:
        scope = "common"
    return db.scalar(
        select(DigitalFact).where(
            DigitalFact.program_id == p.id, DigitalFact.scope_key == scope, DigitalFact.field == key
        )
    )


def value(db, p, key, scope="common", confirmed=True):
    f = fact(db, p, key, scope)
    return (
        f.data.get("value")
        if f and f.data.get("state") == "known" and (not confirmed or f.data.get("confirmed"))
        else None
    )


def allowed_field(db, p, key, scope):
    spec = LOOKUP[key]
    if spec["brand"] and scope not in brands(db, p):
        raise BusinessError("Нужен фактически подключаемый бренд.")
    if not spec["brand"] and scope != "common":
        raise BusinessError("Это общее поле физического предприятия.")
    cond = spec.get("when")
    return not cond or value(db, p, cond[0], scope, False) == cond[1]


def start(db, actor, data, key):
    est = db.get(Entity, data["establishment_id"])
    primary = db.get(Binding, data["primary_binding_id"])
    if (
        not est
        or est.kind != "establishment"
        or not primary
        or primary.purpose != "client"
        or not primary.verified
        or not primary.active
        or primary.bot_id != settings().bot_id
        or primary.partner_id != est.partner_id
        or primary.establishment_id not in {None, est.id}
    ):
        raise BusinessError("Нужны предприятие и его проверенный основной брендовый чат.")
    est = db.scalar(select(Entity).where(Entity.id == est.id).with_for_update())
    old = db.scalar(
        select(DigitalProgram).where(
            DigitalProgram.bot_id == settings().bot_id, DigitalProgram.establishment_id == est.id
        )
    )
    if old:
        authorize(db, actor, old, curator=True)
        return {"id": old.id, "existing": True}
    area = SimpleNamespace(partner_id=est.partner_id, establishment_id=est.id, brand_id=None)
    b = db.get(Binding, actor.binding_id)
    if (
        not b
        or b.purpose != "support"
        or not b.active
        or not b.verified
        or b.bot_id != settings().bot_id
        or not active_person_role(db, actor.person_id, {"curator"}, area)
        or b.brand_id
        or b.partner_id not in {None, est.partner_id}
        or b.establishment_id not in {None, est.id}
    ):
        raise AccessDenied("Паспорт запускает куратор всей площадки в сопровождении.")
    bs = [
        x.brand_id
        for x in db.scalars(
            select(EstablishmentBrand).where(
                EstablishmentBrand.establishment_id == est.id, EstablishmentBrand.active.is_(True)
            )
        )
    ]
    if primary.brand_id not in bs:
        raise BusinessError("Основной чат не относится к активному бренду предприятия.")
    fp = data["partner_person_id"]
    if not active_person_role(db, fp, {"partner"}, SimpleNamespace(**{**area.__dict__, "brand_id": primary.brand_id})):
        raise BusinessError("ФП не назначен на предприятие.")
    due = parse_date(data["due_at"])
    if due <= now():
        raise BusinessError("Нужна будущая дата подключения без просрочки задним числом.")
    project = db.get(ProjectPlan, data.get("project_id")) if data.get("project_id") else None
    if project and (project.establishment_id != est.id or project.bot_id != settings().bot_id):
        raise BusinessError("Проект другого предприятия.")
    dates = project.dates if project else {}
    anchors = []
    if dates.get("planned_technical"):
        anchors.append(parse_date(dates["planned_technical"]) - timedelta(days=5))
    if dates.get("planned_internal"):
        anchors.append(parse_date(dates["planned_internal"]))
    readiness = min(anchors) if anchors else None
    if readiness and readiness > now() and due > readiness:
        raise BusinessError(
            "Срок должен быть не позднее технического открытия минус 5 дней / более раннего внутреннего открытия."
        )
    legacy = db.get(Work, data.get("legacy_work_id")) if data.get("legacy_work_id") else None
    if data.get("legacy_work_id"):
        if (
            not legacy
            or legacy.kind != "digital"
            or legacy.establishment_id != est.id
            or legacy.partner_id != est.partner_id
            or legacy.reviewer_id != actor.person_id
            or legacy.partner_person_id != fp
            or legacy.data.get("digital_program")
        ):
            raise BusinessError("Нужна существующая цифровая работа этого предприятия с назначенным куратором и ФП.")
        w = legacy
        w.data = audit_data(w.data, actor, event="wizard_adopted", previous_status=w.status, previous_outcome=w.outcome)
        w.status = "in_progress"
        w.outcome = None
        w.due_at = due
        w.version += 1
    else:
        w = create_work(
            db,
            actor,
            {
                "kind": "digital",
                "title": ("Цифровое подключение · " + est.name)[:250],
                "partner_id": est.partner_id,
                "establishment_id": est.id,
                "brand_id": primary.brand_id,
                "binding_id": actor.binding_id,
                "client_binding_id": primary.id,
                "owner_id": actor.person_id,
                "reviewer_id": actor.person_id,
                "partner_person_id": fp,
                "due_at": due,
                "calendar": data.get("calendar", "support"),
                "project_id": data.get("project_id"),
                "data": {},
            },
            key,
        )
    p = DigitalProgram(
        bot_id=settings().bot_id,
        establishment_id=est.id,
        work_id=w.id,
        data={
            "brands": bs,
            "primary_binding_id": primary.id,
            "project_ids": [data["project_id"]] if data.get("project_id") else [],
            "readiness_target": readiness.isoformat() if readiness else None,
            "late_input_risk": bool(readiness and readiness <= now()),
            "active": False,
            "master_snapshot": master_snapshot(est),
            "history": [],
        },
    )
    db.add(p)
    db.flush()
    w.data = {**w.data, "digital_program": p.id}
    # Known facts are imported as staff-sourced, never fabricated FP decisions.
    source = {**db.get(Entity, est.partner_id).data, **est.data, **(w.data.get("passport", {}) if legacy else {})}
    if "call_center_applicable" in source and "call_center" not in source:
        source["call_center"] = source["call_center_applicable"]
    for k in (
        "phone",
        "phone_owner",
        "work_phone_messenger",
        "management_messenger",
        "call_center",
        "seating",
        "general_qr",
        "internet",
    ):
        if k in source:
            db.add(
                DigitalFact(
                    program_id=p.id,
                    scope_key="common",
                    field=k,
                    data={
                        "value": source[k],
                        "state": "known",
                        "confirmed": False,
                        "source": "master:" + est.id,
                        "actor": actor.person_id,
                        "at": now().isoformat(),
                        "history": [],
                    },
                )
            )
    ensure_steps(db, p)
    return {"id": p.id, "existing": False}


def ensure_steps(db, p, due=None):
    due = due or root(db, p).original_due_at
    existing = {
        (s.component, s.scope_key) for s in db.scalars(select(DigitalStep).where(DigitalStep.program_id == p.id))
    }
    for key, c in COMPONENTS.items():
        for scope in brands(db, p) if c[5] else ["common"]:
            if (key, scope) not in existing:
                db.add(
                    DigitalStep(
                        program_id=p.id, component=key, scope_key=scope, due_at=due, data={"history": [], "tests": {}}
                    )
                )
    db.flush()


def clean(spec, v):
    from ama.ui.values import required_errors
    from ama.digital_schema import typed

    errors = required_errors([typed(spec["key"])], {"value": v})
    if errors and not (spec["type"] in {"lines", "list", "multi"} and v == []):
        raise BusinessError("; ".join(errors))
    kind = spec["type"]
    if kind == "bool" and not isinstance(v, bool):
        raise BusinessError("Нужно явное Да / Нет.")
    if kind == "choice" and v not in spec["choices"]:
        raise BusinessError("Недопустимый вариант.")
    if kind in {"list", "lines", "multi"} and not isinstance(v, list):
        raise BusinessError("Нужен список.")
    if kind == "multi" and any(x not in spec["choices"] for x in v):
        raise BusinessError("Неизвестный вариант списка.")
    if kind == "object" and not isinstance(v, dict):
        raise BusinessError("Нужен связанный блок полей.")
    if kind in {"text", "url", "ref", "datetime"} and not isinstance(v, str):
        raise BusinessError("Нужно текстовое значение.")
    if kind == "url" and (
        urlsplit(v).scheme != "https" or not urlsplit(v).hostname or urlsplit(v).username or urlsplit(v).password
    ):
        raise BusinessError("Нужна HTTPS-ссылка без пароля.")
    if kind == "datetime":
        parse_date(v)
    if kind in {"list", "object"}:
        allowed = {x["key"] for x in spec.get("fields", [])}
        for child in v if isinstance(v, list) else [v]:
            if not isinstance(child, dict) or set(child) - allowed:
                raise BusinessError("Неизвестные поля вложенного блока.")
    encoded = json.dumps(v, ensure_ascii=False)
    if len(encoded) > 24000:
        raise BusinessError("Слишком большой блок.")
    if re.search(r"(?i)(bot\d{5,}:|\b(?:token|password|пароль|sms.?код|секрет)\s*[:=])", encoded):
        raise BusinessError("Секреты и коды в паспорт не включаются.")
    if spec["key"] in {"phone", "public_phone"} and not re.fullmatch(r"\+?[\d ()-]{7,24}", v):
        raise BusinessError("Проверьте телефон.")
    if spec["key"] == "zones":
        names = set()
        urls = set()
        for z in v:
            u = urlsplit(z["map_url"])
            host = (u.hostname or "").lower()
            if (
                host not in {"yandex.ru", "yandex.com", "yandex.by", "yandex.kz", "maps.yandex.ru"}
                or u.scheme != "https"
                or z["map_url"] in urls
                or z["name"] in names
                or not (parse_qs(u.query).get("um", [""])[0].startswith("constructor") or "map-constructor" in u.path)
            ):
                raise BusinessError("Каждая зона требует отдельной ссылки Яндекс Карт и названия.")
            for n in ("minimum_minor", "delivery_minor", "free_minor", "minutes"):
                if not isinstance(z[n], int) or isinstance(z[n], bool) or z[n] < (1 if n == "minutes" else 0):
                    raise BusinessError("Проверьте числовые условия зоны.")
            names.add(z["name"])
            urls.add(z["map_url"])
    if spec["key"] == "equipment" and any(x.get("state") not in {"ready", "partial", "waiting", "mismatch"} for x in v):
        raise BusinessError("Неизвестное состояние поставки.")
    if spec["key"] == "participants":
        if len({x["contact"] for x in v}) != len(v):
            raise BusinessError("Совмещающий роли участник указывается один раз.")
        roles = {r.lower() for x in v for r in x["roles"]}
        if not {"фп", "управляющий"} <= roles:
            raise BusinessError("Нужны назначенные ФП и Управляющий.")
    return deepcopy(v)


def audit_data(data, actor, **event):
    return {
        **data,
        "history": [
            *data.get("history", []),
            {"actor": actor.person_id, "binding": actor.binding_id, "at": now().isoformat(), **event},
        ],
    }


def save_fact(db, actor, program_id, expected, data):
    p = get(db, program_id, True)
    versioned(p, expected)
    key = data["field"]
    scope = data.get("scope", "common")
    if key not in LOOKUP:
        raise BusinessError("Неизвестное поле паспорта.")
    role = authorize(db, actor, p, write=True, scope=scope)
    if LOOKUP[key].get("staff") and role == "partner":
        raise AccessDenied("Совместимость подтверждает специалист, не ФП.")
    if role == "curator" and LOOKUP[key].get("staff"):
        raise AccessDenied("Фактическую совместимость подтверждает назначенный специалист.")
    if role == "specialist":
        steps = [
            s
            for s in db.scalars(
                select(DigitalStep).where(DigitalStep.program_id == p.id, DigitalStep.work_id.is_not(None))
            )
            if db.get(Work, s.work_id).executor_id == actor.person_id
            and db.get(Work, s.work_id).binding_id == actor.binding_id
        ]
        if key not in {k for s in steps for k in COMPONENTS[s.component][2]}:
            raise AccessDenied("Это поле другого исполнителя.")
    if not allowed_field(db, p, key, scope):
        raise BusinessError("Поле не применяется при текущем ответе. Уточните исходный выбор.")
    f = fact(db, p, key, scope)
    state = data.get("state", "known")
    if state not in {"known", "waiting", "help"}:
        raise BusinessError("Неизвестное состояние ответа.")
    previous = deepcopy(f.data) if f else None
    if state == "known":
        raw = data.get("value")
        file_reference = (
            key in {"price_reference", "menu_reference"} and isinstance(raw, str) and raw.startswith("attachment:")
        )
        if file_reference:
            asset = db.get(Attachment, raw.split(":", 1)[1])
            if (
                not asset
                or asset.work_id != p.work_id
                or asset.status != "stored"
                or asset.visibility != "client"
                or asset.author_id != root(db, p).partner_person_id
            ):
                raise BusinessError("Укажите сохранённый файл ФП из этого цифрового паспорта.")
            v = raw
        else:
            v = clean(LOOKUP[key], raw)
        if key == "zones" and not value(db, p, "delivery_terms", scope):
            raise BusinessError("Сначала ФП подтверждает коммерческие условия доставки.")
        if key == "phone_owner" and not db.get(__import__("ama.models", fromlist=["Identity"]).Identity, v):
            raise BusinessError("Ответственный не найден.")
        if key == "care_calendar":
            cal = db.scalar(
                select(Record).where(Record.kind == "calendar", Record.key == v, Record.published.is_(True))
            )
            if not cal:
                raise BusinessError("Выберите опубликованный календарь.")
            from ama.calendar import Calendar

            Calendar(cal.data)
        if key == "geocards":
            for x in v:
                if x["mode"] not in {"existing", "new"} or (
                    x["mode"] == "existing" and (not x.get("url") or urlsplit(x["url"]).scheme != "https")
                ):
                    raise BusinessError("Для существующей геокарточки нужна HTTPS-ссылка.")
        confirmed = role == "partner" or bool(LOOKUP[key].get("staff") and role == "specialist")
        payload = {
            "state": "known",
            "value": v,
            "confirmed": confirmed,
            "source": "fp" if role == "partner" else "staff",
            "actor": actor.person_id,
            "at": now().isoformat(),
        }
    else:
        control = parse_date(data["next_check"])
        if control <= now() or not data.get("reason") or not data.get("responsible_id"):
            raise BusinessError("Пробел требует причины, ответственного и будущей даты проверки.")
        if not active_person_role(db, data["responsible_id"], {"partner", "curator", "specialist"}, root(db, p)):
            raise BusinessError("Ответственный не назначен.")
        payload = {
            "state": state,
            "value": None,
            "confirmed": False,
            "reason": data["reason"],
            "responsible_id": data["responsible_id"],
            "next_check": control.isoformat(),
            "actor": actor.person_id,
            "at": now().isoformat(),
        }
    if not f:
        f = DigitalFact(program_id=p.id, scope_key=scope, field=key, data={})
        db.add(f)
    payload["history"] = [
        *f.data.get("history", []),
        {
            "previous": {k: v for k, v in (previous or {}).items() if k != "history"},
            "actor": actor.person_id,
            "binding": actor.binding_id,
            "at": now().isoformat(),
        },
    ]
    f.data = payload
    f.version = (f.version or 0) + 1
    p.version += 1
    # Children made inapplicable are preserved in history but cannot supply readiness.
    affected = invalidate(db, p, key, scope, actor)
    p.data = {**p.data, "cursor": {"field": key, "scope": scope, "actor": actor.person_id}}
    if state == "help":
        help_request(db, actor, p, key, scope, data["reason"], control)
    db.flush()
    return {"id": p.id, "field": key, "affected": affected}


def fields_for_step(db, p, s):
    result = set(COMPONENTS[s.component][2])
    if s.component.startswith("agg_"):
        platform = s.component[4:]
        delivery = value(db, p, platform + "_delivery", s.scope_key) if platform != "chibis" else "enterprise"
        if delivery == "enterprise":
            result |= {"delivery_terms", "zones"}
    return {
        k
        for k in result
        if not LOOKUP[k].get("when") or value(db, p, LOOKUP[k]["when"][0], s.scope_key, False) == LOOKUP[k]["when"][1]
    }


def invalidate(db, p, key, scope, actor):
    affected = []
    seeds = []
    for s in db.scalars(select(DigitalStep).where(DigitalStep.program_id == p.id)):
        # Use complete conditional field set here so changing a parent cannot leave a stale result.
        keys = COMPONENTS[s.component][2] | (
            {"zones", "delivery_terms"}
            if s.component.startswith("agg_")
            and (
                s.component == "agg_chibis"
                or value(db, p, s.component[4:] + "_delivery", s.scope_key, False) == "enterprise"
            )
            else set()
        )
        master_affected = key == "@master" and s.component not in {"equipment", "screens"}
        if (key in keys or master_affected) and (scope == "common" or s.scope_key == scope):
            seeds.append(s)
    while seeds:
        s = seeds.pop(0)
        if s.id in affected:
            continue
        affected.append(s.id)
        if s.status in {"accepted", "review"}:
            s.data = audit_data(
                s.data,
                actor,
                event="invalidated",
                field=key,
                scope=scope,
                previous_status=s.status,
                tests=s.data.get("tests", {}),
            )
            s.status = "rework"
            s.version += 1
            if s.work_id:
                w = db.get(Work, s.work_id)
                if w.status == "done":
                    w.status = "in_progress"
                    w.outcome = None
                    w.owner_id = w.executor_id
                    w.version += 1
        for other in db.scalars(select(DigitalStep).where(DigitalStep.program_id == p.id)):
            if s.component in COMPONENTS[other.component][3] and (
                s.scope_key == "common" or other.scope_key == s.scope_key
            ):
                seeds.append(other)
    if affected:
        p.data = {**p.data, "ready": False}
        w = root(db, p)
        if w.status == "done":
            w.status = "in_progress"
            w.outcome = None
            w.version += 1
    return affected


def missing(db, p, s):
    result = []
    for k in sorted(fields_for_step(db, p, s)):
        scope = s.scope_key if LOOKUP[k]["brand"] else "common"
        if value(db, p, k, scope) is None:
            result.append(k)
    if s.scope_key != "common" and value(db, p, "own_delivery", s.scope_key) is False:
        if "own_delivery" in (value(db, p, "methods", s.scope_key) or []):
            result.append("conflict:own_delivery_method")
        if (
            s.component.startswith("agg_")
            and s.component != "agg_chibis"
            and value(db, p, s.component[4:] + "_delivery", s.scope_key) == "enterprise"
        ):
            result.append("conflict:platform_requires_enterprise_delivery")
    if s.component == "agg_chibis" and value(db, p, "own_delivery", s.scope_key) is False:
        result.append("conflict:chibis_without_own_delivery")
    if s.component in {"table_qr"} and value(db, p, "seating") is False:
        result.append("conflict:no_seating")
    if s.component == "equipment" and (
        value(db, p, "internet") != "ready"
        or value(db, p, "equipment_approved") is not True
        or not value(db, p, "equipment")
        or any(x["state"] != "ready" for x in value(db, p, "equipment") or [])
    ):
        result.append("hardware_or_network_not_ready")
    if s.component in {"ama_rs", "care"}:
        if value(db, p, "phone_ready") is not True or value(db, p, "account_ready") is not True:
            result.append("working_account_not_ready")
    if s.component == "table_qr" and value(db, p, "table_qr") is not True:
        result.append("table_qr_not_selected")
    if s.component == "general_qr" and value(db, p, "general_qr") is not True:
        result.append("general_qr_not_selected")
    if s.component == "screens" and not value(db, p, "screens"):
        result.append("no_installed_screens")
    if s.component.startswith("payment_") and s.component[8:] not in (value(db, p, "payments") or []):
        result.append("payment_not_selected")
    if s.component in {"sellkit"} or s.component.startswith("agg_"):
        for pay in value(db, p, "payments") or []:
            x = db.scalar(
                select(DigitalStep).where(
                    DigitalStep.program_id == p.id,
                    DigitalStep.component == "payment_" + pay,
                    DigitalStep.scope_key == "common",
                )
            )
            if not x or x.mode == "na" or (x.status != "accepted" and not x.data.get("deferred")):
                result.append("payment:" + pay)
        own_model = (
            s.component == "sellkit"
            or s.component == "agg_chibis"
            or (
                s.component.startswith("agg_")
                and value(db, p, s.component[4:] + "_delivery", s.scope_key) == "enterprise"
            )
        )
        if value(db, p, "own_delivery", s.scope_key) is True and own_model:
            z = db.scalar(
                select(DigitalStep).where(
                    DigitalStep.program_id == p.id,
                    DigitalStep.component == "zones",
                    DigitalStep.scope_key == s.scope_key,
                )
            )
            if not z or z.mode == "na" or z.status != "accepted":
                result.append("accepted_zones")
    for dep in COMPONENTS[s.component][3]:
        ds = "common" if not COMPONENTS[dep][5] else s.scope_key
        x = db.scalar(
            select(DigitalStep).where(
                DigitalStep.program_id == p.id, DigitalStep.component == dep, DigitalStep.scope_key == ds
            )
        )
        if not x or x.mode == "na" or x.status != "accepted":
            result.append("dependency:" + dep)
    return result


def state(db, p, s):
    if s.mode == "na":
        return "not_applicable"
    if s.mode == "unconfirmed":
        return "configuration"
    if s.status in {"accepted", "review", "waiting_external", "rework"}:
        return s.status
    gaps = missing(db, p, s)
    if any(x.startswith(("dependency:", "payment:", "accepted_")) for x in gaps):
        return "waiting_dependency"
    if gaps:
        return "needs_data"
    return "in_progress" if s.work_id else "ready"


def task_payload(db, p, s):
    est = db.get(Entity, p.establishment_id)
    return {
        "enterprise": {
            "id": est.id,
            "name": est.name,
            "address": est.data.get("address"),
            "city": est.data.get("city"),
            "hours": est.data.get("hours"),
            "coordinates": est.data.get("coordinates"),
        },
        "brand_id": s.scope_key if s.scope_key != "common" else None,
        "actual_brands": [s.scope_key] if s.scope_key != "common" else None,
        "project_ids": p.data.get("project_ids", []),
        "external_routing": {
            "primary_client_binding_id": p.data["primary_binding_id"],
            "partner_person_id": root(db, p).partner_person_id,
            "curator_id": root(db, p).reviewer_id,
        }
        if s.component in {"ama_rs", "care"}
        else None,
        "component": s.component,
        "mode": s.mode,
        "due_at": s.due_at.isoformat(),
        "facts": {
            k: {
                "value": value(db, p, k, s.scope_key, False),
                "confirmed": bool(fact(db, p, k, s.scope_key) and fact(db, p, k, s.scope_key).data.get("confirmed")),
                "source": fact(db, p, k, s.scope_key).data.get("source") if fact(db, p, k, s.scope_key) else None,
            }
            for k in sorted(fields_for_step(db, p, s))
        },
        "missing": missing(db, p, s),
        "tests": list(COMPONENTS[s.component][4]),
    }


def configure(db, actor, program_id, expected, data):
    p = get(db, program_id, True)
    versioned(p, expected)
    authorize(db, actor, p, curator=True)
    s = db.get(DigitalStep, data["step_id"])
    if not s or s.program_id != p.id:
        raise BusinessError("Шаг другого паспорта.")
    mode = data["mode"]
    due = parse_date(data.get("due_at") or s.due_at.isoformat() + "Z")
    if mode not in {"connect", "verify", "na"} or not data.get("reason") or due <= now():
        raise BusinessError("Нужны применимость, основание и будущая дата.")
    if mode == "na" and s.component in {"care", "ama_rs", "equipment", "iiko"}:
        raise BusinessError("Обязательную систему нельзя исключить из готовности.")
    if s.work_id and s.status not in {"needs_data", "ready"} and not data.get("confirm_change"):
        raise BusinessError("Начатый шаг меняется только с явным согласованием.")
    s.data = audit_data(
        s.data,
        actor,
        event="configuration",
        previous_mode=s.mode,
        previous_due=s.due_at.isoformat(),
        reason=data["reason"],
        deferred=bool(data.get("deferred")),
    )
    s.mode = mode
    s.due_at = due
    s.version += 1
    if data.get("deferred") and s.component in {"care", "ama_rs", "iiko", "equipment"}:
        raise BusinessError("Обязательный результат нельзя исключить из стартовой готовности переносом.")
    if data.get("deferred") and not data.get("approval"):
        raise BusinessError("Поздний запуск требует согласованного основания.")
    s.data = {**s.data, "deferred": bool(data.get("deferred")), "approval": data.get("approval")}
    if s.work_id:
        w = db.get(Work, s.work_id)
        if w.data.get("paid_started_at") or w.data.get("offer_accepted"):
            raise BusinessError("Оплаченная работа меняется отдельным согласованием.")
    p.version += 1
    p.data = {**p.data, "ready": False}
    if s.status in {"accepted", "review"}:
        s.data = audit_data(s.data, actor, event="configuration_recheck", previous_status=s.status)
        s.status = "rework"
    if root(db, p).status == "done":
        root(db, p).status = "in_progress"
        root(db, p).outcome = None
        root(db, p).version += 1
    return {"id": p.id}


def plan_snapshot(db, p):
    result = []
    for s in db.scalars(select(DigitalStep).where(DigitalStep.program_id == p.id).order_by(DigitalStep.id)):
        if s.mode == "unconfirmed":
            raise BusinessError(
                "Подтвердите применимость всех строк реестра; отсутствие данных не означает «не применяется»."
            )
        if s.mode == "na" or s.work_id:
            continue
        purpose = COMPONENTS[s.component][1]
        candidates = []
        area = SimpleNamespace(
            partner_id=root(db, p).partner_id,
            establishment_id=p.establishment_id,
            brand_id=None if s.scope_key == "common" else s.scope_key,
        )
        for r in db.scalars(
            select(Record).where(Record.kind == "route", Record.key == purpose, Record.published.is_(True))
        ):
            b = db.get(Binding, r.data.get("binding_id"))
            if (
                b
                and b.bot_id == p.bot_id
                and b.active
                and b.verified
                and b.purpose == purpose
                and all(
                    getattr(b, k) in {None, getattr(area, k)} for k in ("partner_id", "establishment_id", "brand_id")
                )
                and active_person_role(db, r.data.get("owner_id"), {"specialist"}, area)
            ):
                candidates.append((r, b))
        if len(candidates) != 1:
            raise BusinessError("Нужен один опубликованный маршрут с назначенным специалистом: " + purpose)
        r, b = candidates[0]
        result.append(
            {
                "step": s.id,
                "version": s.version,
                "route": r.id,
                "route_version": r.version,
                "binding": b.id,
                "binding_version": b.version,
                "executor": r.data["owner_id"],
                "payload": task_payload(db, p, s),
            }
        )
    return result


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def activate(db, actor, program_id, expected, data):
    p = get(db, program_id, True)
    versioned(p, expected)
    authorize(db, actor, p, curator=True)
    snapshot = plan_snapshot(db, p)
    if data.get("_fingerprint") and data["_fingerprint"] != fingerprint(snapshot):
        raise Conflict("План, вводные или маршруты изменились; повторите предпросмотр.")
    for item in snapshot:
        s = db.get(DigitalStep, item["step"])
        r = db.get(Record, item["route"])
        scope = None if s.scope_key == "common" else s.scope_key
        primary = db.get(Binding, p.data["primary_binding_id"])
        client = primary if not scope else client_binding(db, p, scope)
        # A single executor queue topic carries several independent step results.
        group = next(
            (
                db.get(Work, x.work_id)
                for x in db.scalars(
                    select(DigitalStep).where(DigitalStep.program_id == p.id, DigitalStep.work_id.is_not(None))
                )
                if db.get(Work, x.work_id).executor_id == item["executor"]
                and db.get(Work, x.work_id).data.get("digital_route") == item["binding"]
                and db.get(Work, x.work_id).brand_id == scope
            ),
            None,
        )
        if not group:
            group = Work(
                kind="digital_group",
                title=("Цифровой пакет · " + db.get(Entity, p.establishment_id).name)[:250],
                partner_id=root(db, p).partner_id,
                establishment_id=p.establishment_id,
                brand_id=scope,
                owner_id=item["executor"],
                executor_id=item["executor"],
                reviewer_id=root(db, p).reviewer_id,
                partner_person_id=root(db, p).partner_person_id,
                binding_id=item["binding"],
                review_binding_id=root(db, p).binding_id,
                client_binding_id=client.id,
                parent_id=p.work_id,
                project_id=root(db, p).project_id,
                due_at=s.due_at,
                original_due_at=s.due_at,
                data={
                    "digital_program": p.id,
                    "digital_route": item["binding"],
                    "calendar": r.data.get("calendar", "support"),
                },
            )
            db.add(group)
            db.flush()
            from ama.sla import initialize

            initialize(db, group)
            b = db.get(Binding, item["binding"])
            if b.platform == "telegram":
                db.add(
                    Outbox(
                        dedupe_key="digital-topic:" + group.id,
                        platform=b.platform,
                        chat_id=b.chat_id,
                        topic_key=None,
                        binding_id=b.id,
                        binding_version=b.version,
                        payload={"op": "create_topic", "topic_name": group.title, "digital_group": group.id},
                    )
                )
            else:
                queue(db, b, "digital-group:" + group.id, "Цифровой пакет " + group.id, work=group)
        s.work_id = group.id
        s.data = {**s.data, "input_snapshot": item["payload"]}
        s.version += 1
    p.version += 1
    p.data = {**p.data, "active": True}
    return {"id": p.id, "steps": len(snapshot), "fingerprint": fingerprint(snapshot)}


def client_binding(db, p, brand):
    items = list(
        db.scalars(
            select(Binding).where(
                Binding.bot_id == p.bot_id,
                Binding.purpose == "client",
                Binding.partner_id == root(db, p).partner_id,
                Binding.brand_id == brand,
                Binding.active.is_(True),
                Binding.verified.is_(True),
            )
        )
    )
    items = [b for b in items if b.establishment_id in {None, p.establishment_id}]
    if len(items) != 1:
        raise BusinessError("Нужен однозначный клиентский маршрут бренда.")
    return items[0]


def on_topic(db, row, external_id):
    w = db.get(Work, row.payload["digital_group"])
    p = get(db, w.data["digital_program"])
    source = db.get(Binding, row.binding_id)
    if w.data.get("digital_topic"):
        return
    if not external_id or not str(external_id).isdigit():
        raise BusinessError("Не получен ID темы.")
    b = Binding(
        bot_id=p.bot_id,
        platform="telegram",
        chat_id=source.chat_id,
        topic_key=str(external_id),
        purpose=source.purpose,
        active=True,
        verified=True,
        partner_id=source.partner_id,
        brand_id=source.brand_id,
        establishment_id=source.establishment_id,
    )
    db.add(b)
    db.flush()
    w.binding_id = b.id
    w.data = {**w.data, "digital_topic": b.id}
    queue(
        db,
        b,
        "digital-topic-card:" + w.id,
        "Цифровое подключение · " + w.id + "\n/digital — отдельные результаты и недостающие вводные.",
        work=w,
    )


def result(db, actor, program_id, expected, data):
    p = get(db, program_id, True)
    versioned(p, expected)
    authorize(db, actor, p)
    s = db.get(DigitalStep, data["step_id"])
    if not s or s.program_id != p.id or not s.work_id or s.mode == "na":
        raise BusinessError("Нет активного задания.")
    w = db.get(Work, s.work_id)
    if (
        w.executor_id != actor.person_id
        or w.binding_id != actor.binding_id
        or not active_person_role(db, actor.person_id, {"specialist"}, w)
    ):
        raise AccessDenied("Результат сообщает назначенный специалист в своей теме.")
    if s.status == "accepted":
        raise BusinessError("Результат уже принят; изменение оформляется отдельно.")
    gaps = missing(db, p, s)
    if gaps:
        raise BusinessError("Не выполнены условия: " + ", ".join(gaps))
    if re.search(
        r"(?i)(bot\d{5,}:|\b(?:token|password|пароль|sms.?код|секрет)\s*[:=])", json.dumps(data, ensure_ascii=False)
    ):
        raise BusinessError("Секреты и коды не включаются в результат.")
    tests = data.get("tests", {})
    needed = set(COMPONENTS[s.component][4])
    if value(db, p, "work_phone_messenger") == value(db, p, "management_messenger"):
        needed.discard("cross_platform")
    if not needed <= set(tests) or any(
        not isinstance(tests[k], dict) or tests[k].get("passed") is not True or not tests[k].get("evidence")
        for k in needed
    ):
        raise BusinessError(
            "Нужен отдельный успешный контрольный тест каждого применимого направления: " + ", ".join(sorted(needed))
        )
    if not data.get("evidence") or not data.get("actual_configuration") or not data.get("tested_at"):
        raise BusinessError("Нужны фактическая конфигурация, подтверждение и дата теста.")
    if parse_date(data["tested_at"]) > now():
        raise BusinessError("Тест не может быть в будущем.")
    if s.component in {"sellkit"} or s.component.startswith("agg_"):
        if not data.get("published_at") or parse_date(data["published_at"]) > now():
            raise BusinessError("Подготовленная витрина не равна опубликованному каналу; укажите факт доступности.")
    if s.component == "care":
        sources = value(db, p, "care_sources", s.scope_key) or []
        results = data.get("source_results", {})
        if any(
            not isinstance(results.get(k), dict)
            or results[k].get("passed") is not True
            or not results[k].get("evidence")
            for k in sources
        ):
            raise BusinessError("Подтвердите отдельный тест каждого применимого источника Службы заботы.")
    if s.component == "geo":
        results = data.get("platform_results", {})
        if any(
            not isinstance(results.get(x["platform"]), dict)
            or results[x["platform"]].get("passed") is not True
            or not results[x["platform"]].get("url")
            for x in value(db, p, "geocards", s.scope_key) or []
        ):
            raise BusinessError("Нужны отдельные ссылки и проверки каждой геоплощадки.")
    if s.component == "care" and not data.get("external_ids"):
        raise BusinessError("Исполнитель указывает проверенные ID и гостевые маршруты Службы заботы.")
    s.data = audit_data(s.data, actor, event="result", previous_tests=s.data.get("tests", {}))
    s.data = {
        **s.data,
        "tests": deepcopy(tests),
        "evidence": data["evidence"],
        "actual_configuration": data["actual_configuration"],
        "tested_at": data["tested_at"],
        "external_ids": data.get("external_ids"),
        "published_at": data.get("published_at"),
        "source_results": data.get("source_results", {}),
        "platform_results": data.get("platform_results", {}),
        "remarks": data.get("remarks", ""),
        "input_fingerprint": fingerprint(task_payload(db, p, s)),
    }
    s.status = "review"
    s.version += 1
    p.version += 1
    if w.status == "new":
        from ama.service import transition

        transition(db, actor, w.id, w.version, "take", {}, "digital-take:" + s.id + ":" + str(s.version))
    queue(
        db,
        db.get(Binding, w.review_binding_id),
        "digital-result:" + s.id + ":" + str(s.version),
        "На проверке: " + COMPONENTS[s.component][0] + " · " + s.id,
        work=w,
    )
    return {"id": p.id, "step_id": s.id}


def review(db, actor, program_id, expected, data):
    p = get(db, program_id, True)
    versioned(p, expected)
    authorize(db, actor, p, curator=True)
    s = db.get(DigitalStep, data["step_id"])
    if not s or s.program_id != p.id or s.status != "review":
        raise BusinessError("Шаг не на приёмке.")
    w = db.get(Work, s.work_id)
    if actor.person_id == w.executor_id:
        raise AccessDenied("Исполнитель не принимает собственную работу за куратора.")
    if data["accept"]:
        if missing(db, p, s) or s.data.get("input_fingerprint") != fingerprint(task_payload(db, p, s)):
            raise Conflict("Вводные изменились: требуется новый контрольный результат.")
        if s.data.get("remarks"):
            raise BusinessError("Сначала устраните блокирующие замечания.")
        s.status = "accepted"
    else:
        if not data.get("reason"):
            raise BusinessError("Нужна причина возврата.")
        due = parse_date(data["due_at"])
        if due <= now():
            raise BusinessError("Нужна будущая дата доработки.")
        s.status = "rework"
        s.due_at = due
    s.data = audit_data(
        s.data,
        actor,
        event="accept" if data["accept"] else "reject",
        reason=data.get("reason"),
        tests=s.data.get("tests", {}),
    )
    s.version += 1
    p.version += 1
    siblings = list(db.scalars(select(DigitalStep).where(DigitalStep.work_id == w.id)))
    if all(x.mode == "na" or x.status == "accepted" for x in siblings):
        w.status = "done"
        w.outcome = "accepted"
        w.version += 1
    return {"id": p.id, "status": s.status}


def wait(db, actor, program_id, expected, data):
    p = get(db, program_id, True)
    versioned(p, expected)
    authorize(db, actor, p)
    s = db.get(DigitalStep, data["step_id"])
    w = db.get(Work, s.work_id) if s and s.work_id else None
    if not s or s.program_id != p.id or not w or actor.person_id != w.executor_id or actor.binding_id != w.binding_id:
        raise AccessDenied("Нет назначения на этот шаг.")
    due = parse_date(data["next_check"])
    if due <= now() or not data.get("party") or not data.get("condition"):
        raise BusinessError("Ожидание требует условия, стороны и будущей проверки.")
    if s.status in {"accepted", "review"}:
        raise BusinessError("Принятый / сданный результат не заменяется ожиданием.")
    s.data = audit_data(
        s.data, actor, event="wait", party=data["party"], condition=data["condition"], next_check=due.isoformat()
    )
    s.data = {**s.data, "wait": {"party": data["party"], "condition": data["condition"], "next_check": due.isoformat()}}
    s.status = "waiting_external"
    s.version += 1
    p.version += 1
    return {"id": p.id}


def complete(db, actor, program_id, expected, data):
    p = get(db, program_id, True)
    versioned(p, expected)
    authorize(db, actor, p, curator=True)
    pending = [
        s
        for s in db.scalars(select(DigitalStep).where(DigitalStep.program_id == p.id))
        if s.mode == "unconfirmed"
        or s.mode != "na"
        and (s.status != "accepted" or missing(db, p, s))
        and not s.data.get("deferred")
    ]
    for s in db.scalars(select(DigitalStep).where(DigitalStep.program_id == p.id, DigitalStep.status == "accepted")):
        if s.mode != "na" and s.data.get("input_fingerprint") != fingerprint(task_payload(db, p, s)):
            pending.append(s)
    assignment = p.data.get("course_assignment")
    if value(db, p, "call_center") is True and (
        not assignment
        or not db.scalar(
            select(Training.id).where(
                Training.person_id == assignment["person_id"],
                Training.course_key == assignment["course_key"],
                Training.course_version == assignment["course_version"],
                Training.completed_at.is_not(None),
            )
        )
    ):
        raise BusinessError(
            "Для выбранного КЦ назначьте опубликованный курс конкретному человеку и проверьте его завершение."
        )
    payments = value(db, p, "payments")
    if not payments:
        pending.append("no_confirmed_payment_method")
    if pending or not data.get("evidence"):
        raise BusinessError("Есть непринятые обязательные результаты / отсутствует подтверждение итоговой проверки.")
    w = root(db, p)
    w.data = {
        **w.data,
        "components": {
            s.id: {"confirmed": s.status == "accepted", "component": s.component}
            for s in db.scalars(select(DigitalStep).where(DigitalStep.program_id == p.id))
            if s.mode != "na" and not s.data.get("deferred")
        },
        "digital_readiness": {"actor": actor.person_id, "at": now().isoformat(), "evidence": data["evidence"]},
    }
    w.data = {**w.data, "required_components": list(w.data["components"])}
    w.status = "done"
    w.outcome = "accepted"
    w.version += 1
    p.version += 1
    p.data = audit_data(p.data, actor, event="readiness", evidence=data["evidence"])
    p.data = {**p.data, "ready": True}
    return {"id": p.id, "ready": True}


def before_transition(db, actor, w, action, data):
    if not w.data.get("digital_program"):
        return
    if action in {"submit", "accept", "reject", "cancel", "handoff", "reschedule"}:
        raise BusinessError(
            "Используйте отдельные результаты, приёмку и сроки в цифровом мастере; пакет не закрывается массовым действием."
        )


def help_request(db, actor, p, key, scope, text, control):
    ident = scope + ":" + key
    saved = p.data.get("help", {})
    old = db.get(Work, saved.get(ident)) if saved.get(ident) else None
    if old:
        old.data = {
            **old.data,
            "questions": [
                *old.data.get("questions", []),
                {"text": text, "actor": actor.person_id, "at": now().isoformat()},
            ],
        }
        return old
    w = root(db, p)
    client = db.get(Binding, p.data["primary_binding_id"]) if scope == "common" else client_binding(db, p, scope)
    q = Work(
        kind="digital_question",
        title=("Уточнение цифрового паспорта: " + LOOKUP[key]["label"])[:250],
        partner_id=w.partner_id,
        establishment_id=p.establishment_id,
        brand_id=client.brand_id,
        binding_id=w.binding_id,
        client_binding_id=client.id,
        owner_id=w.reviewer_id,
        executor_id=w.reviewer_id,
        reviewer_id=w.reviewer_id,
        partner_person_id=w.partner_person_id,
        due_at=control,
        original_due_at=control,
        parent_id=w.id,
        data={
            "calendar": w.data.get("calendar", "support"),
            "questions": [{"text": text, "actor": actor.person_id, "at": now().isoformat()}],
        },
    )
    db.add(q)
    db.flush()
    p.data = {**p.data, "help": {**saved, ident: q.id}}
    queue(db, db.get(Binding, w.binding_id), "digital-help:" + q.id, "Нужна помощь: " + q.title + "\n" + text, work=q)
    return q


def add_brand(db, actor, program_id, expected, data):
    p = get(db, program_id, True)
    versioned(p, expected)
    authorize(db, actor, p, curator=True)
    brand = data["brand_id"]
    if not db.scalar(
        select(EstablishmentBrand.id).where(
            EstablishmentBrand.establishment_id == p.establishment_id,
            EstablishmentBrand.brand_id == brand,
            EstablishmentBrand.active.is_(True),
        )
    ):
        raise BusinessError("Нет активного бренда на площадке.")
    due = parse_date(data["due_at"]) if data.get("due_at") else None
    if due is None or due <= now():
        raise BusinessError("Новая брендовая часть требует будущей даты подключения.")
    if brand not in p.data["brands"]:
        p.data = audit_data(p.data, actor, event="brand_added", brand=brand, reason=data.get("reason"))
        p.data = {**p.data, "brands": [*p.data["brands"], brand], "ready": False}
        ensure_steps(db, p, due)
        p.version += 1
    return {"id": p.id}


def call_center_course(db, actor, program_id, expected, data):
    p = get(db, program_id, True)
    versioned(p, expected)
    authorize(db, actor, p, curator=True)
    person = data["person_id"]
    course = db.scalar(
        select(Record).where(Record.kind == "course", Record.key == data["course_key"], Record.published.is_(True))
    )
    if (
        not course
        or course.key != "call_center"
        or not course.data.get("cards")
        or not active_person_role(db, person, {"partner", "curator"}, root(db, p))
    ):
        raise BusinessError("Нужны назначенный человек и опубликованный курс КЦ.")
    item = db.scalar(
        select(Training).where(
            Training.person_id == person, Training.course_key == course.key, Training.course_version == course.version
        )
    )
    if not item:
        item = Training(
            person_id=person,
            course_key=course.key,
            course_version=course.version,
            cards=deepcopy(course.data["cards"]),
            position=0,
        )
        db.add(item)
    p.data = audit_data(p.data, actor, event="course_assigned", person=person, course=course.key)
    p.data = {
        **p.data,
        "course_assignment": {"person_id": person, "course_key": course.key, "course_version": course.version},
    }
    p.version += 1
    target = db.get(
        Binding, root(db, p).client_binding_id if person == root(db, p).partner_person_id else actor.binding_id
    )
    queue(
        db,
        target,
        "digital-course:" + p.id + ":" + str(p.version),
        "Назначен персональный короткий курс КЦ · "
        + db.get(__import__("ama.models", fromlist=["Identity"]).Identity, person).name
        + '\nОткрыть: /training {"course":"call_center"}',
        work=root(db, p),
    )
    return {"id": p.id, "course": course.key, "person": person}


def request(db, actor, program_id, expected, data):
    """One scoped missing-fact request, using delivery-anchored existing SLA."""
    p = get(db, program_id, True)
    versioned(p, expected)
    authorize(db, actor, p, curator=True)
    key = data["field"]
    scope = data.get("scope", "common")
    if key not in LOOKUP or not allowed_field(db, p, key, scope):
        raise BusinessError("Выберите применимое поле.")
    w = root(db, p)
    if w.status == "done":
        raise BusinessError("Сначала оформите изменение принятого паспорта.")
    from ama.service import transition

    # Root client route is not changed when a brand-specific request is made.
    target = db.get(Binding, p.data["primary_binding_id"]) if scope == "common" else client_binding(db, p, scope)
    if target.id != w.client_binding_id or w.owner_id != actor.person_id:
        # Separate per-brand question preserves the correct SLA recipient and all object scope.
        q = help_request(
            db, actor, p, key, scope, data.get("reason", "Нужны исходные данные"), parse_date(data["next_check"])
        )
    else:
        q = w
    field_label = LOOKUP[key]["label"]
    return_work = transition(
        db,
        actor,
        q.id,
        q.version,
        "wait_partner",
        {
            "party": "ФП",
            "required": field_label + " · " + db.get(Entity, p.establishment_id).name,
            "reason": data.get("reason") or "Без подтверждения нельзя принять зависимый цифровой результат.",
            "next_check": data["next_check"],
        },
        "digital-request:" + p.id + ":" + str(p.version),
    )
    p.version += 1
    return {"id": p.id, "work_id": return_work.id}


def tick(db):
    """Aggregate one blocker/control per real cause, leaving original deadlines intact."""
    from ama import sla
    from ama.models import SLAEvent, SLASignal

    changed = False
    for p in db.scalars(select(DigitalProgram).where(DigitalProgram.bot_id == settings().bot_id)):
        if not p.data.get("active"):
            continue
        p = get(db, p.id, True)
        w = root(db, p)
        causes = {}
        est = db.get(Entity, p.establishment_id)
        current_master = master_snapshot(est)
        if p.data.get("master_snapshot") != current_master:
            actor = Actor(w.reviewer_id, w.review_binding_id or w.binding_id)
            invalidate(db, p, "@master", "common", actor)
            p.data = audit_data(
                p.data, actor, event="master_changed", previous=p.data.get("master_snapshot"), current=current_master
            )
            p.data = {**p.data, "master_snapshot": current_master}
            p.version += 1
            changed = True
        for f in db.scalars(select(DigitalFact).where(DigitalFact.program_id == p.id)):
            if f.data.get("state") in {"waiting", "help"} and parse_date(f.data["next_check"]) <= now():
                causes["fact:" + f.scope_key + ":" + f.field] = f.data["reason"]
        for s in db.scalars(select(DigitalStep).where(DigitalStep.program_id == p.id)):
            if s.mode in {"na", "unconfirmed"} or s.status == "accepted":
                continue
            gaps = missing(db, p, s)
            if s.work_id and gaps:
                # The same input blocker is reported once, rather than one late-result signal per dependent system.
                db.query(SLAEvent).filter(
                    SLAEvent.work_id == s.work_id,
                    SLAEvent.kind.in_(["result", "before_result", "result_day"]),
                    SLAEvent.status.in_(["pending", "blocked"]),
                ).update({"status": "cancelled"}, synchronize_session=False)
            wait_ = s.data.get("wait", {})
            control = (
                parse_date(wait_["next_check"])
                if s.status == "waiting_external" and wait_.get("next_check")
                else s.due_at
            )
            if control > now():
                continue
            for gap in gaps or ["result:" + str(s.work_id or s.id)]:
                if gap.startswith(("dependency:", "payment:", "accepted_")):
                    continue
                scope = "common" if gap in LOOKUP and not LOOKUP[gap]["brand"] else s.scope_key
                causes[scope + ":" + gap] = wait_.get("condition") or ("Не принят цифровой результат: " + gap)
        c = sla.case_for(db, w)
        if c:
            for cause, reason in causes.items():
                event_key = "digital:" + p.id + ":" + fingerprint(cause)[:24]
                if db.scalar(select(SLASignal.id).where(SLASignal.event_key == "sla-signal:" + event_key)):
                    continue
                changed = True
                sla.signal(
                    db,
                    c,
                    w,
                    "digital:" + p.id + ":" + fingerprint(cause)[:24],
                    reason,
                    curator_only=True,
                    responsible_id=w.reviewer_id,
                )
    return changed


def deliverable(db, row):
    context = row.payload.get("digital_file")
    if context:
        try:
            p = get(db, context["program_id"])
            _, f, asset = file_for_executor(
                db, Actor(context["actor_id"], context["binding_id"]), p, context["step_id"], context["field"]
            )
            return f.version == context["fact_version"] and asset.id == row.payload.get("attachment_id")
        except (BusinessError, AccessDenied):
            return False
    if not row.payload.get("digital_group"):
        return True
    w = db.get(Work, row.payload["digital_group"])
    if not w:
        return False
    p = get(db, w.data["digital_program"])
    b = db.get(Binding, row.binding_id)
    return bool(
        b
        and b.bot_id == p.bot_id
        and b.active
        and b.verified
        and b.version == row.binding_version
        and active_person_role(db, w.executor_id, {"specialist"}, w)
        and active_person_role(db, root(db, p).reviewer_id, {"curator"}, root(db, p))
    )


def master_snapshot(est):
    return {k: est.data.get(k) for k in ("address", "city", "hours", "coordinates")}


def replace_binding(db, old, new):
    """Administrative same-scope replacement preserves passport/proofs and history."""
    for p in db.scalars(select(DigitalProgram).where(DigitalProgram.bot_id == settings().bot_id)):
        if p.data.get("primary_binding_id") == old.id:
            p.data = {
                **p.data,
                "primary_binding_id": new.id,
                "history": [
                    *p.data.get("history", []),
                    {"event": "binding_alias", "old": old.id, "new": new.id, "at": now().isoformat()},
                ],
            }
            p.version += 1
    for w in db.scalars(select(Work).where(Work.kind == "digital_group")):
        changes = {k: new.id for k in ("digital_route", "digital_topic") if w.data.get(k) == old.id}
        if changes:
            w.data = {**w.data, **changes}


def course_access(db, actor, course_key):
    """Course access is personal and tied to this object's confirmed KC service."""
    if course_key != "call_center":
        return False
    b = db.get(Binding, actor.binding_id)
    if not b or b.bot_id != settings().bot_id or not b.active or not b.verified:
        return False
    for p in db.scalars(select(DigitalProgram).where(DigitalProgram.bot_id == settings().bot_id)):
        assignment = p.data.get("course_assignment", {})
        if (
            assignment.get("person_id") != actor.person_id
            or assignment.get("course_key") != course_key
            or value(db, p, "call_center") is not True
        ):
            continue
        w = root(db, p)
        if any(
            getattr(b, k) and getattr(b, k) != getattr(w, k) for k in ("partner_id", "establishment_id", "brand_id")
        ):
            continue
        if b.purpose == "client":
            if (
                b.partner_id != w.partner_id
                or b.brand_id not in brands(db, p)
                or b.establishment_id not in {None, p.establishment_id}
            ):
                continue
            area = SimpleNamespace(partner_id=w.partner_id, establishment_id=p.establishment_id, brand_id=b.brand_id)
            if active_person_role(db, actor.person_id, {"partner"}, area):
                return True
        elif (
            actor.person_id == w.reviewer_id
            and b.purpose == "support"
            and active_person_role(db, actor.person_id, {"curator"}, w)
        ):
            return True
    return False


def file_for_executor(db, actor, p, step_id, field):
    authorize(db, actor, p)
    s = db.get(DigitalStep, step_id)
    if not s or s.program_id != p.id or not s.work_id or field not in fields_for_step(db, p, s):
        raise AccessDenied("Файл другого направления.")
    task = db.get(Work, s.work_id)
    binding = db.get(Binding, actor.binding_id)
    if (
        binding.purpose == "client"
        or actor.person_id != task.executor_id
        or actor.binding_id != task.binding_id
        or not active_person_role(db, actor.person_id, {"specialist"}, task)
    ):
        raise AccessDenied("Утверждённый файл получает назначенный исполнитель в своей теме.")
    f = fact(db, p, field, s.scope_key)
    raw = value(db, p, field, s.scope_key)
    if not isinstance(raw, str) or not raw.startswith("attachment:"):
        raise BusinessError("В этом поле нет подтверждённого файла.")
    asset = db.get(Attachment, raw.split(":", 1)[1])
    if (
        not asset
        or asset.work_id != p.work_id
        or asset.author_id != root(db, p).partner_person_id
        or asset.status != "stored"
        or asset.visibility != "client"
    ):
        raise BusinessError("Исходный файл недоступен.")
    return s, f, asset


def send_file(db, actor, program_id, step_id, field):
    p = get(db, program_id)
    s, f, asset = file_for_executor(db, actor, p, step_id, field)
    row = queue(
        db,
        db.get(Binding, actor.binding_id),
        "digital-file:" + s.id + ":" + f.id + ":" + str(f.version) + ":" + actor.binding_id,
        "Утверждённый документ · " + LOOKUP[field]["label"],
        work=db.get(Work, s.work_id),
        attachment_id=asset.id,
    )
    row.payload = {
        **row.payload,
        "digital_file": {
            "program_id": p.id,
            "step_id": s.id,
            "field": field,
            "fact_version": f.version,
            "actor_id": actor.person_id,
            "binding_id": actor.binding_id,
        },
    }
    return row
