"""Persistent, scoped digital wizard on the existing draft/preview engine."""

from sqlalchemy import select
from datetime import timedelta
from ama import digital as d
from ama.models import DigitalProgram, DigitalStep, Binding, Work, Entity, Attachment, Identity
from ama.digital_schema import FIELDS, LOOKUP, COMPONENTS, MODES, typed
from ama.errors import AccessDenied, BusinessError
from ama.ui import business, access
from ama.ui.engine import button as B, render
from ama.ui.controller import show, paginate, navigation, draft_screen
from ama.ui.schema import f, ref, choice

STATES = {
    "configuration": "Подтвердить применимость",
    "not_applicable": "Не применяется",
    "needs_data": "Нужны данные",
    "waiting_dependency": "Ожидает зависимости",
    "ready": "Готов к передаче",
    "in_progress": "В работе",
    "waiting_external": "Ждём внешнюю сторону",
    "review": "На проверке",
    "rework": "Нужна доработка",
    "accepted": "Принято",
}


def fields(kind):
    action = kind.split(":")[1]
    if action == "start":
        return [
            ref("establishment_id", "Предприятие", "establishment", True),
            ref("primary_binding_id", "Основной клиентский чат предприятия", "digital_client", True),
            ref("partner_person_id", "Назначенный ФП", "person", True),
            ref("project_id", "Связанный проект", "project_plan"),
            ref("legacy_work_id", "Подключить существующую цифровую работу (необязательно)", "work"),
            ref("calendar", "Календарь", "record:calendar", True),
            f("due_at", "Будущая дата цифровой готовности", "datetime", True),
        ]
    if action == "fact":
        key = kind.split(":")[2]
        return (
            [f("value", LOOKUP[key]["label"] + " / attachment:ID сохранённого файла", "text", True)]
            if key in {"price_reference", "menu_reference"}
            else [typed(key)]
        )
    if action == "request":
        return [f("reason", "Причина запроса", required=True), f("next_check", "Следующая проверка", "datetime", True)]
    if action == "gap":
        return [
            f("reason", "Что отсутствует / какая помощь нужна", required=True),
            ref("responsible_id", "Следующий ответственный", "person", True),
            f("next_check", "Дата следующей проверки", "datetime", True),
        ]
    if action == "configure":
        return [
            choice("mode", "Применимость", MODES, True),
            f("reason", "Основание выбора / изменения", required=True),
            f("due_at", "Срок результата", "datetime", True),
            f("confirm_change", "Явно согласовано изменение начатого шага", "bool"),
            f("deferred", "Согласованный поздний запуск", "bool"),
            f("approval", "Основание позднего запуска"),
        ]
    if action == "result":
        test_fields = [
            f("passed", "Тест успешен", "bool", True),
            f("evidence", "Что проверено и фактический результат", required=True),
        ]
        return [
            f("actual_configuration", "Фактическая конфигурация", required=True),
            f("evidence", "Подтверждение результата", required=True),
            f("tested_at", "Фактическая дата проверки", "datetime", True),
            f(
                "tests",
                "Проверки",
                "list",
                True,
                fields=[f("key", "Ключ проверки из карточки", required=True), *test_fields],
            ),
            f("external_ids", "Проверенные внешние ID / гостевые маршруты"),
            f("published_at", "Фактическая доступность витрины / площадки", "datetime"),
            f(
                "source_results",
                "Проверки источников Службы заботы",
                "list",
                fields=[
                    f("key", "Источник", required=True),
                    f("passed", "Успешно", "bool", True),
                    f("evidence", "Результат проверки", required=True),
                ],
            ),
            f(
                "platform_results",
                "Геоплощадки",
                "list",
                fields=[
                    f("key", "Площадка", required=True),
                    f("passed", "Проверено", "bool", True),
                    f("url", "Фактическая ссылка", "url", True),
                ],
            ),
            f("remarks", "Нерешённые замечания"),
        ]
    if action == "review":
        return [
            f("accept", "Принять результат", "bool", True),
            f("reason", "Причина возврата"),
            f("due_at", "Новый срок при возврате", "datetime"),
        ]
    if action == "wait":
        return [
            f("party", "Чья сторона должна действовать", required=True),
            f("condition", "Ожидаемое условие", required=True),
            f("next_check", "Следующая проверка", "datetime", True),
        ]
    if action == "complete":
        return [f("evidence", "Фактическое подтверждение итоговой готовности", required=True)]
    if action == "add_brand":
        return [
            f("due_at", "Будущая дата подключения бренда", "datetime", True),
            ref("brand_id", "Новый фактический бренд", "brand", True),
            f("reason", "Основание", required=True),
        ]
    if action == "course":
        return [
            ref("person_id", "Кому назначается курс КЦ", "person", True),
            ref("course_key", "Опубликованный короткий курс", "record:course", True),
        ]
    return []


def visible(db, actor):
    result = []
    for p in db.scalars(select(DigitalProgram).where(DigitalProgram.bot_id == d.settings().bot_id)):
        try:
            d.authorize(db, actor, p)
            result.append(p)
        except AccessDenied:
            pass
    return result


def step_list(db, actor, p):
    role = d.authorize(db, actor, p)
    b = db.get(Binding, actor.binding_id)
    rows = list(
        db.scalars(
            select(DigitalStep)
            .where(DigitalStep.program_id == p.id)
            .order_by(DigitalStep.scope_key, DigitalStep.component)
        )
    )
    if role in {"client", "partner"}:
        rows = [x for x in rows if x.scope_key in {"common", b.brand_id}]
    if role == "specialist":
        rows = [
            x
            for x in rows
            if x.work_id
            and db.get(Work, x.work_id).executor_id == actor.person_id
            and db.get(Work, x.work_id).binding_id == actor.binding_id
        ]
    return rows


def begin(db, actor, s, p, action, data=None):
    draft = business.make_draft(db, actor, "digital:" + action, "Цифровое подключение", data or {}, p)
    if not fields(draft.kind):
        from ama.ui.controller import dispatch

        return dispatch(db, actor, s, "preview", {"draft_id": draft.id})
    return draft_screen(db, actor, s, {"draft_id": draft.id})


def dispatch(db, actor, s, action, x):
    if action == "dg_list":
        items, page = paginate(visible(db, actor), x.get("page", 0))
        all_items = visible(db, actor)
        buttons = [B(db.get(Entity, p.establishment_id).name, "dg_view", id=p.id) for p in items]
        b = db.get(Binding, actor.binding_id)
        if b.purpose == "support" and access.staff_role(db, actor, "curator"):
            buttons.insert(0, B("Начать подготовку", "dg_start"))
        buttons += navigation("dg_list", page, len(all_items)) + [B("В меню", "home")]
        return show(db, actor, s, "Цифровые подключения предприятий. Выберите адрес.", buttons, action, page=page)
    if action == "dg_start":
        return begin(db, actor, s, None, "start", {"calendar": "support"})
    p = d.get(db, x["id"])
    role = d.authorize(db, actor, p)
    client = db.get(Binding, actor.binding_id).purpose == "client"
    if action == "dg_view":
        est = db.get(Entity, p.establishment_id)
        steps = step_list(db, actor, p)
        items, page = paginate(steps, x.get("page", 0))
        lines = [
            f"{est.name} · {est.data.get('city', '')} · {est.data.get('address', '')}",
            "Цифровое подключение. Принятый паспорт не означает готовность систем.",
        ]
        lines += [
            f"{COMPONENTS[z.component][0]}: {STATES[d.state(db, p, z)]}"
            + (" · ПРОСРОЧЕНО" if z.mode != "na" and z.status != "accepted" and z.due_at < d.now() else "")
            for z in items
        ]
        buttons = [
            B(
                COMPONENTS[z.component][0]
                + (" · " + db.get(Entity, z.scope_key).name if z.scope_key != "common" else " · общее"),
                "dg_step",
                id=p.id,
                step_id=z.id,
            )
            for z in items
        ]
        buttons += [
            B(
                "Вопросы паспорта / продолжить",
                "dg_facts",
                id=p.id,
                scope="common",
            )
        ]
        if role == "curator":
            buttons += [
                B("Предпросмотр плана задач", "dg_begin", id=p.id, op="activate"),
                B("Итоговая приёмка", "dg_begin", id=p.id, op="complete"),
                B("Добавить бренд площадки", "dg_begin", id=p.id, op="add_brand"),
                B("Курс КЦ конкретному человеку", "dg_begin", id=p.id, op="course"),
            ]
        buttons += navigation("dg_view", page, len(steps), id=p.id) + [B("Список предприятий", "dg_list")]
        if p.data.get("late_input_risk"):
            lines.append("РИСК: исходные данные поступили позже рубежа подготовки; даты не пересозданы задним числом.")
        if p.data.get("readiness_target"):
            lines.append("Рубеж подготовки UTC: " + p.data["readiness_target"])
        return show(db, actor, s, "\n".join(lines), buttons, action, id=p.id, page=page)
    if action == "dg_facts":
        scope = x.get("scope", "common")
        if client and scope not in {"common", db.get(Binding, actor.binding_id).brand_id}:
            raise AccessDenied("Другой бренд.")
        if scope not in {"common", *d.brands(db, p)}:
            raise BusinessError("Неизвестная область.")
        allowed = set(LOOKUP)
        if role == "specialist":
            allowed = {k for z in step_list(db, actor, p) for k in d.fields_for_step(db, p, z)}
        scoped_steps = step_list(db, actor, p)
        if any(z.mode != "unconfirmed" for z in scoped_steps):
            active_keys = {
                k for z in scoped_steps if z.mode not in {"na", "unconfirmed"} for k in d.fields_for_step(db, p, z)
            }
            allowed &= active_keys
        fields_ = [
            f
            for f in FIELDS
            if f["key"] in allowed
            and not (client and f.get("staff"))
            and (f["brand"] == (scope != "common"))
            and d.allowed_field(db, p, f["key"], scope)
        ]
        # Missing/unconfirmed facts first; known confirmed answers are only edited explicitly.
        fields_.sort(key=lambda f: d.value(db, p, f["key"], scope) is not None)
        items, page = paginate(fields_, x.get("page", 0))
        lines = ["Общие поля площадки" if scope == "common" else db.get(Entity, scope).name]
        buttons = []
        for f_ in items:
            item = d.fact(db, p, f_["key"], scope)
            state = (
                (
                    item.data.get("state")
                    + (" · подтверждено" if item.data.get("confirmed") else " · требует подтверждения ФП")
                )
                if item
                else "нет данных"
            )
            lines.append(f_["label"] + ": " + state)
            buttons.append(B(f_["label"], "dg_fact", id=p.id, field=f_["key"], scope=scope))
        buttons += navigation("dg_facts", page, len(fields_), id=p.id, scope=scope)
        for sc in ["common", *d.brands(db, p)]:
            if sc != scope and (not client or sc in {"common", db.get(Binding, actor.binding_id).brand_id}):
                buttons.append(
                    B("Общие поля" if sc == "common" else db.get(Entity, sc).name, "dg_facts", id=p.id, scope=sc)
                )
        if (
            client
            and actor.person_id == d.root(db, p).partner_person_id
            and actor.binding_id == d.root(db, p).client_binding_id
        ):
            buttons.append(B("Добавить файл прайса / меню", "dg_upload", id=p.id))
            buttons.append(
                B(
                    "Выбрать сохранённый прайс",
                    "dg_files",
                    id=p.id,
                    scope=db.get(Binding, actor.binding_id).brand_id,
                    field="price_reference",
                )
            )
            buttons.append(
                B(
                    "Выбрать сохранённое меню",
                    "dg_files",
                    id=p.id,
                    scope=db.get(Binding, actor.binding_id).brand_id,
                    field="menu_reference",
                )
            )
        buttons.append(B("Реестр результатов", "dg_view", id=p.id))
        return show(db, actor, s, "\n".join(lines), buttons, action, id=p.id, scope=scope, page=page)
    if action in {"dg_files", "dg_use_file"}:
        d.authorize(db, actor, p, write=True, scope=x["scope"])
        if x["field"] not in {"price_reference", "menu_reference"} or not d.allowed_field(
            db, p, x["field"], x["scope"]
        ):
            raise BusinessError("Сначала выберите индивидуальный прайс / меню.")
        items = list(
            db.scalars(
                select(Attachment).where(
                    Attachment.work_id == p.work_id,
                    Attachment.author_id == d.root(db, p).partner_person_id,
                    Attachment.visibility == "client",
                    Attachment.status == "stored",
                )
            )
        )
        if action == "dg_use_file":
            if x["attachment_id"] not in {a.id for a in items}:
                raise AccessDenied("Нет сохранённого файла ФП.")
            return begin(
                db,
                actor,
                s,
                p,
                "fact:" + x["field"],
                {"field": x["field"], "scope": x["scope"], "value": "attachment:" + x["attachment_id"]},
            )
        page_items, page = paginate(items, x.get("page", 0))
        return show(
            db,
            actor,
            s,
            "Выберите утверждённый файл этого паспорта. Внутренние файлы сотрудников в список не входят.",
            [
                B(a.filename, "dg_use_file", id=p.id, field=x["field"], scope=x["scope"], attachment_id=a.id)
                for a in page_items
            ]
            + navigation("dg_files", page, len(items), id=p.id, field=x["field"], scope=x["scope"])
            + [B("Назад", "dg_facts", id=p.id, scope=x["scope"])],
            action,
            **x,
        )
    if action == "dg_upload":
        d.authorize(db, actor, p, write=True)
        w = d.root(db, p)
        if actor.binding_id != w.client_binding_id:
            raise AccessDenied(
                "Файл паспорта загружается из основного клиентского чата; в другом бренде можно передать утверждённую ссылку."
            )
        if w.status == "done":
            raise BusinessError("Сначала согласуйте изменение принятого паспорта.")
        return render(
            db,
            actor,
            s,
            "Отправьте утверждённый прайс / меню. После сохранения используйте attachment:ID из подтверждения загрузки в соответствующем поле. Внутренние файлы автоматически наружу не отправляются.",
            [B("К паспорту", "dg_facts", id=p.id, scope="common")],
            prompt={
                "type": "attachment",
                "work_id": w.id,
                "work_version": w.version,
                "expires": (d.now() + timedelta(minutes=15)).isoformat(),
            },
        )
    if action == "dg_fact":
        key = x["field"]
        scope = x["scope"]
        d.authorize(db, actor, p, scope=scope)
        if key not in LOOKUP or not d.allowed_field(db, p, key, scope):
            raise BusinessError("Поле не применяется.")
        item = d.fact(db, p, key, scope)
        if (
            item
            and role == "specialist"
            and key not in {k for z in step_list(db, actor, p) for k in d.fields_for_step(db, p, z)}
        ):
            raise AccessDenied("Поле другого направления.")
        text = (
            LOOKUP[key]["label"]
            + "\n"
            + (
                ("Текущее значение: " + str(item.data.get("value")) + "\nИсточник: " + str(item.data.get("source")))
                if item
                else "Данных пока нет."
            )
        )
        buttons = [
            B("Подтвердить / изменить", "dg_answer", id=p.id, field=key, scope=scope),
            B("Пока нет", "dg_gap", id=p.id, field=key, scope=scope, state="waiting"),
            B("Нужна помощь", "dg_gap", id=p.id, field=key, scope=scope, state="help"),
            B("Сохранить и продолжить позже", "dg_facts", id=p.id, scope=scope),
        ]
        if role == "curator":
            buttons.insert(0, B("Запросить у ФП", "dg_begin", id=p.id, op="request", field=key, scope=scope))
        return show(db, actor, s, text, buttons, action, **x)
    if action == "dg_answer":
        d.authorize(db, actor, p, write=True, scope=x["scope"])
        item = d.fact(db, p, x["field"], x["scope"])
        return begin(
            db,
            actor,
            s,
            p,
            "fact:" + x["field"],
            {"field": x["field"], "scope": x["scope"], "value": item.data.get("value") if item else None},
        )
    if action == "dg_gap":
        return begin(db, actor, s, p, "gap", {"field": x["field"], "scope": x["scope"], "state": x["state"]})
    if action == "dg_get_file":
        d.send_file(db, actor, p.id, x["step_id"], x["field"])
        return show(
            db,
            actor,
            s,
            "Утверждённый документ поставлен в очередь вашей рабочей темы.",
            [B("К шагу", "dg_step", id=p.id, step_id=x["step_id"])],
            action,
            **x,
        )
    if action == "dg_step":
        z = db.get(DigitalStep, x["step_id"])
        if not z or z.id not in {i.id for i in step_list(db, actor, p)}:
            raise AccessDenied("Нет доступа к шагу.")
        gaps = d.missing(db, p, z)
        lines = [
            COMPONENTS[z.component][0] + " · " + STATES[d.state(db, p, z)],
            "Применимость: " + MODES.get(z.mode, "Не подтверждена"),
            f"Срок UTC: {z.due_at.isoformat()}",
            "Недостаёт: " + (", ".join(LOOKUP[k]["label"] if k in LOOKUP else k for k in gaps) or "нет"),
            "Проверки: " + ", ".join(COMPONENTS[z.component][4]),
        ]
        task = db.get(Work, z.work_id) if z.work_id else None
        executor = db.get(Identity, task.executor_id) if task else None
        responsible = (
            d.root(db, p).reviewer_id
            if z.status == "review" or not task
            else (
                d.root(db, p).partner_person_id
                if any(k in LOOKUP and not LOOKUP[k].get("staff") for k in gaps)
                else task.executor_id
            )
        )
        person = db.get(Identity, responsible)
        lines.append("Исполнитель: " + (executor.name if executor else "План ещё не передан"))
        lines.append("Следующее действие: " + (person.name if person else "Куратор"))
        if z.data.get("wait"):
            lines.append("Ожидание: " + str(z.data["wait"]))
        if z.data.get("tests") and not client:
            lines.append("Фактические проверки: " + str(z.data["tests"]))
        if not client:
            lines.append("Подпаспорт: " + str(d.task_payload(db, p, z)))
        buttons = []
        if role == "curator":
            buttons.append(B("Применимость / срок", "dg_begin", id=p.id, op="configure", step_id=z.id))
            if z.status == "review":
                buttons.append(B("Принять / вернуть", "dg_begin", id=p.id, op="review", step_id=z.id))
        if role == "specialist":
            for field in d.fields_for_step(db, p, z):
                val = d.value(db, p, field, z.scope_key)
                if isinstance(val, str) and val.startswith("attachment:"):
                    buttons.append(
                        B("Получить: " + LOOKUP[field]["label"], "dg_get_file", id=p.id, step_id=z.id, field=field)
                    )
            buttons += [
                B("Результат и контрольные тесты", "dg_begin", id=p.id, op="result", step_id=z.id),
                B("Ждём внешнюю сторону", "dg_begin", id=p.id, op="wait", step_id=z.id),
            ]
        buttons += [
            B("Вводные / недостающие факты", "dg_facts", id=p.id, scope=z.scope_key),
            B("Назад к реестру", "dg_view", id=p.id),
        ]
        return show(db, actor, s, "\n".join(lines), buttons, action, **x)
    if action == "dg_begin":
        data = {k: v for k, v in x.items() if k not in {"id", "op"}}
        if "step_id" in data:
            z = db.get(DigitalStep, data["step_id"])
            if not z or z.program_id != p.id:
                raise BusinessError("Шаг другого паспорта.")
            if x["op"] == "configure":
                data.update(mode=z.mode if z.mode != "unconfirmed" else None, due_at=z.due_at.isoformat() + "Z")
            if x["op"] == "result":
                data["tests"] = [{"key": k, "passed": False, "evidence": ""} for k in COMPONENTS[z.component][4]]
        return begin(db, actor, s, p, x["op"], data)
    raise BusinessError("Неизвестное действие цифрового мастера.")
