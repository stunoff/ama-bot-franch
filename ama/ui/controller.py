"""Button-driven administration and typed, persistent editors."""
from copy import deepcopy
from datetime import timedelta
from sqlalchemy import select, func
from ama import admin
from ama.config import settings
from ama.errors import AccessDenied, BusinessError
from ama.models import (UIDraft, Record, Entity, Role, Binding, EstablishmentBrand,
                        ConfigurationRevision, Inbox, Outbox, Attachment, Timer, now, uid)
from ama.ui import access, business, repository as repo, schema, values
from ama.ui.engine import button as B, render, view_draft, update, apply_field, PAGE_SIZE

SECTIONS = {
    "chats": ("Чаты и маршруты", ["bindings", "record:route"]),
    "people": ("Сотрудники и роли", ["people", "accounts", "roles"]),
    "entities": ("ФП, предприятия и бренды", ["partner", "establishment", "brand", "links"]),
    "scenarios": ("Сценарии и чек-листы", ["policy:scenario", "policy:generic"]),
    "sales": ("Продажи", ["policy:source", "policy:faq", "policy:sales_content", "record:commercial", "record:consent"]),
    "training": ("Ознакомление ФП", ["record:course"]),
    "materials": ("Материалы и сообщения", ["record:material", "record:template"]),
    "calendars": ("Календари и сроки", ["record:calendar", "policy:sla"]),
    "payments": ("Платные работы", ["policy:payment"]),
    "schedules": ("Расписания и отчёты", ["policy:schedule","policy:broadcast_permission"]),
}
MODEL_CATALOG = {"binding": "bindings", "person": "people", "account": "accounts", "role": "roles", "link": "links"}


def title(key):
    return repo.POLICY_LABELS[key.split(":")[1]] if key.startswith("policy:") else repo.ADMIN_CATALOGS[key][0]


def navigation(action, page, total, **params):
    result = []
    if page:
        result.append(B("← Предыдущая страница", action, page=page-1, **params))
    if (page+1)*PAGE_SIZE < total:
        result.append(B("Следующая страница →", action, page=page+1, **params))
    return result


def paginate(items, page):
    page = max(0, min(int(page), max(0, (len(items)-1)//PAGE_SIZE)))
    return items[page*PAGE_SIZE:(page+1)*PAGE_SIZE], page


def show(db, actor, s, text, actions, action="home", **params):
    return render(db, actor, s, text, actions, view={"action": action, "params": params})


def defaults(fields):
    result = {}
    for f in fields:
        if f["type"] == "object" and f["required"]:
            result[f["key"]] = defaults(f.get("fields", []))
        elif f["type"] in {"list", "multi", "multiref", "lines", "dates", "integers"}:
            result[f["key"]] = []
        elif f["type"] == "bool":
            result[f["key"]] = False
    return result


def new_draft(db, actor, kind, data=None, target=None):
    if business.configuration(kind):
        admin.check_admin(db, actor)
    else:
        access.require_staff(db, actor)
    initial = defaults(schema.fields_for(kind))
    if kind == "record:policy:sales_content":
        initial.setdefault("value", {})["public"] = True
    if kind == "account":
        initial["active"] = True
    initial.update(data or {})
    if kind.startswith("record:policy:"):
        initial.setdefault("value", {})["policy_type"] = kind.split(":")[2]
    if kind == "create_work":
        initial = {"kind": "support", "priority": "normal", "owner_id": actor.person_id,
                   "reviewer_id": actor.person_id, "binding_id": actor.binding_id, **initial}
    if kind == "export":
        today = (now()+timedelta(hours=3)).date()
        initial = {"report": "works", "start": today.replace(day=1).isoformat(), "end": today.isoformat(), **initial}
    return business.make_draft(db, actor, kind, "Новая запись" if business.configuration(kind) else {
        "create_work": "Новая работа", "project": "Состав проекта", "broadcast": "Информационная публикация", "export": "Выгрузка",
    }.get(kind, kind), initial, target=target)


def object_for(db, actor, catalog, object_id):
    obj = next((r for r in repo.catalog(db, actor, catalog) if r.id == object_id), None)
    if not obj:
        raise BusinessError("Запись не найдена в этом справочнике.")
    return obj


def field_label(db, spec, value):
    if spec["type"] == "ref":
        return repo.reference_label(db, spec["source"], value)
    if spec["type"] == "multiref":
        return ", ".join(repo.reference_label(db, spec["source"], v) for v in value or []) or "не выбрано"
    if spec["type"] == "multi":
        return ", ".join(spec["choices"].get(str(v), str(v)) for v in value or []) or "не выбрано"
    return values.display(value, spec)


def content_lines(db, kind, data, path=None):
    """Human field names and values, including nested cards, instead of JSON dumps."""
    path = path or []
    spec = schema.descriptor(kind, data, path)
    obj = schema.value_at(data, path, {}) if path else data
    if spec["type"] == "list":
        lines = []
        for i, entry in enumerate(obj or []):
            lines.append(f"{i+1}. " + str(entry.get("title") or entry.get("key") or "Пункт"))
        return lines
    if not isinstance(obj, dict):
        return [field_label(db, spec, obj)]
    fields = spec.get("fields", [])
    known = {f["key"] for f in fields}
    fields = fields + [schema.infer(k, v) for k, v in obj.items() if k not in known and k not in {"command", "policy_type", "knowledge_id", "knowledge_version"}]
    return [f"{f['label']}: {field_label(db, f, obj.get(f['key']))}" for f in fields if f["key"] != "command"]


def draft_screen(db, actor, s, p):
    d = view_draft(db, actor, p["draft_id"])
    if d.status not in {"draft", "saved", "checked"}:
        return show(db, actor, s, f"{d.title}\nСостояние: {d.status}. Изменения этого черновика уже завершены.", [B("Мои черновики", "drafts"), B("В меню", "home")])
    path, page = p.get("path", []), int(p.get("page", 0))
    spec = schema.descriptor(d.kind, d.data, path)
    value = schema.value_at(d.data, path, {}) if path else d.data
    text = f"{d.title}\nЧерновик · версия {d.version}\n" + (" → ".join(str(v+1) if isinstance(v, int) else str(v) for v in path) + "\n" if path else "")
    actions = []
    if spec["type"] == "list":
        items, page = paginate(list(enumerate(value or [])), page)
        for i, item in items:
            actions.append(B(f"{i+1}. {item.get('title') or item.get('key') or 'Новый пункт'}", "draft", draft_id=d.id, path=path+[i]))
        actions += navigation("draft", page, len(value or []), draft_id=d.id, path=path)
        actions.append(B("+ Добавить пункт", "list_add", draft_id=d.id, path=path))
        text += f"Пунктов: {len(value or [])}."
    else:
        value = value or {}
        fields = spec.get("fields", [])
        known = {f["key"] for f in fields}
        fields = fields + [schema.infer(k, v) for k, v in value.items() if k not in known and k not in {"command", "policy_type", "commercial_version", "knowledge_id", "knowledge_version"}]
        items, page = paginate(fields, page)
        for field in items:
            label = field["label"] + (" *" if field["required"] else "")
            current = field_label(db, field, value.get(field["key"]))
            text += f"\n{label}: {current[:220]}"
            actions.append(B(label, "field", draft_id=d.id, path=path+[field["key"]]))
        actions += navigation("draft", page, len(fields), draft_id=d.id, path=path)
        if d.kind == "record:policy:generic" and path[:2] == ["value", "settings"]:
            actions.append(B("+ Новый параметр", "extra_key", draft_id=d.id, path=path))
    if d.kind in {"packet:create","packet:revise","packet:schedule"} and not path:
        actions.append(B("Добавить файлы / фото пакета", "pk_asset", draft_id=d.id))
    if path and isinstance(path[-1], int):
        actions += [B("↑ Выше", "list_move", draft_id=d.id, path=path, delta=-1), B("↓ Ниже", "list_move", draft_id=d.id, path=path, delta=1),
                    B("Удалить пункт из черновика", "list_delete", draft_id=d.id, path=path)]
    if path:
        actions.append(B("Сохранить черновик", "draft_save", draft_id=d.id))
        actions.append(B("← К группе полей", "draft", draft_id=d.id, path=path[:-1]))
        actions.append(B("К началу формы", "draft", draft_id=d.id))
    else:
        if d.kind.startswith("record:"):
            current = db.get(Record, d.target_id) if d.target_id else None
            actions.append(B("Проверить и опубликовать…", "preview", draft_id=d.id, publish=True))
            if not current or not current.published:
                actions.append(B("Проверить и сохранить запись…", "preview", draft_id=d.id, publish=False))
        else:
            actions.append(B("Проверить и продолжить…", "preview", draft_id=d.id))
        actions += [B("Сохранить черновик", "draft_save", draft_id=d.id), B("Отменить черновик…", "draft_cancel", draft_id=d.id)]
    actions.append(B("В меню", "home"))
    return show(db, actor, s, text + "\n* — обязательное поле. Ввод сохраняется автоматически.", actions, "draft", draft_id=d.id, path=path, page=page)


def field_screen(db, actor, s, p):
    d = view_draft(db, actor, p["draft_id"])
    if d.status not in {"draft", "saved", "checked"}:
        raise BusinessError("Черновик уже завершён.")
    path, page, search = p["path"], int(p.get("page", 0)), p.get("search", "")
    spec = schema.descriptor(d.kind, d.data, path)
    if spec["type"] in {"object", "list"}:
        return draft_screen(db, actor, s, {"draft_id": d.id, "path": path})
    value = schema.value_at(d.data, path)
    text = spec["label"] + "\nТекущее значение: " + field_label(db, spec, value)
    actions = []
    multi = spec["type"] in {"multi", "multiref"}
    if spec["type"] in {"ref", "multiref", "multi", "choice", "bool"}:
        options = repo.refs(db, actor, spec["source"], d, search) if spec["type"] in {"ref", "multiref"} else list(spec.get("choices", {}).items()) if spec["type"] != "bool" else [(True, "Да"), (False, "Нет")]
        if d.kind == "create_work" and path == ["kind"] and not any(r.name in {"curator", "support_head"} and access.staff_role(db, actor, r.name) for r in access.assignments(db, actor)):
            options = [(k, v) for k, v in options if k.startswith("marketing") or k in {"design", "smm", "advertising", "printing"}]
        if d.kind == "export" and path == ["report"] and not repo.is_admin(db, actor):
            options = [(k, v) for k, v in options if k != "audit"]
        if spec.get("integer"):
            options = [(int(k), v) for k, v in options]
        items, page = paginate(options, page)
        for key, label in items:
            selected = key in (value or []) if multi else key == value
            actions.append(B(("✓ " if selected else "") + label, "field_set", draft_id=d.id, path=path, value=key, multi=multi, page=page, search=search))
        actions += navigation("field", page, len(options), draft_id=d.id, path=path, search=search)
        if spec["type"] in {"ref", "multiref"}:
            actions.append(B("Найти вариант", "search", target="field", target_params={"draft_id": d.id, "path": path}))
        prompt = None
        text += "\nВыберите несколько вариантов; ✓ отмечает выбранные." if multi else "\nВыберите значение."
    else:
        prompt = {"type": "field", "draft_id": d.id, "draft_version": d.version, "path": path, "expires": (now()+timedelta(minutes=15)).isoformat()}
        text += "\nПришлите значение следующим сообщением."
        text += {"datetime": "\nФормат: 25.12.2026 15:00 (часовой пояс предприятия, по умолчанию Москва).",
                 "date": "\nФормат: 25.12.2026.", "money": "\nСумма в рублях, например 1250,50.",
                 "lines": "\nКаждый пункт — с новой строки.", "timezone": "\nНапример Europe/Moscow."}.get(spec["type"], "")
    if not spec["required"]:
        actions.append(B("Очистить", "field_clear", draft_id=d.id, path=path))
    actions += [B("Готово / назад", "draft", draft_id=d.id, path=path[:-1]), B("В меню", "home")]
    return render(db, actor, s, text, actions, view={"action": "field", "params": p}, prompt=prompt)


def config_command(db, actor, obj, command, data=None, kind=None):
    admin.check_admin(db, actor)
    return business.make_draft(db, actor, "config:" + command, {
        "probe_binding": "Тест доставки", "activate_binding": "Подключение чата", "replace_binding": "Замена чата",
        "disable_binding": "Отключение чата", "revoke_role": "Отзыв назначения", "archive_record": "Архивация записи",
        "link_update": "Изменение связи бренда", "retry_delivery": "Повтор доставки", "retry_inbox": "Повтор обработки",
        "retry_attachment": "Повтор сохранения файла", "retry_timer": "Повтор контроля срока", "reconcile_delivery": "Сверка неопределённой доставки",
    }.get(command, "Административное действие"), {"command": {"type": command, "data": {
        "id": obj.key if isinstance(obj, Inbox) else obj.id, "version": getattr(obj, "version", None), **(data or {})}}, "reason": ""}, target=None,
        before={"description": repo.label(db, obj)})


def dispatch(db, actor, s, action, p):
    if action.startswith("pk_"):
        from ama.ui.packets import dispatch as packet_ui
        return packet_ui(db,actor,s,action,p)
    if action.startswith("sla_"):
        from ama.ui.sla import dispatch as sla_ui
        return sla_ui(db,actor,s,action,p)
    if action.startswith("pp_"):
        from ama.ui.projects import dispatch as project_ui
        return project_ui(db,actor,s,action,p)
    if action == "home":
        if db.get(Binding,actor.binding_id).purpose == "client":
            return show(db,actor,s,"Решение сохранено. /start — главное меню.",[B("Мои проекты","pp_list")])
        if repo.is_admin(db, actor) and not access.staff(db, actor):
            return dispatch(db, actor, s, "settings", {})
        from ama.ui.workspace import dispatch as workspace
        return workspace(db, actor, s, "home", p)
    if action == "settings":
        admin.check_admin(db, actor)
        count = db.scalar(select(func.count()).select_from(ConfigurationRevision))
        published = db.scalar(select(func.count()).select_from(Record).where(Record.published.is_(True)))
        errors = db.scalar(select(func.count()).select_from(Outbox).where(Outbox.status.in_(["failed", "blocked", "uncertain", "sending"])))
        drafts = db.scalar(select(func.count()).select_from(UIDraft).where(UIDraft.bot_id == settings().bot_id,
                          UIDraft.person_id == actor.person_id, UIDraft.binding_id == actor.binding_id, UIDraft.status.in_(["draft", "saved", "checked"])))
        actions = [B(label, "section", section=k) for k, (label, _) in SECTIONS.items()]
        actions += [B("Журнал и диагностика", "diagnostics"), B("История версий", "history"), B("Проверить настройки", "check"), B("Мои черновики", "drafts")]
        if access.staff(db, actor):
            actions.append(B("Рабочее место", "workspace"))
        return show(db, actor, s, f"Настройки АМА Франчайзинг\nРедакций изменений: {count}. Опубликованных записей: {published}.\nОшибок доставки / сверок: {errors}. Моих черновиков: {drafts}.\nВыберите раздел.", actions, "settings")
    if action == "section":
        admin.check_admin(db, actor)
        label, keys = SECTIONS[p["section"]]
        actions = [B(title(k), "catalog", catalog=k) for k in keys]
        if p["section"] == "people":
            actions += [B("Добавить сотрудника с аккаунтом и ролью", "new", kind="employee"), B("Замещение ответственного…", "new", kind="substitution")]
        return show(db, actor, s, label, actions+[B("← Настройки", "settings")], "section", **p)
    if action == "catalog":
        key, search = p["catalog"], p.get("search", "")
        rows = repo.catalog(db, actor, key, search)
        items, page = paginate(rows, p.get("page", 0))
        actions = [B(repo.label(db, r), "object", catalog=key, object_id=r.id) for r in items]
        actions += navigation("catalog", page, len(rows), catalog=key, search=search)
        kind = "record:" + key if key.startswith("policy:") else key if key.startswith("record:") else {
            "bindings": "binding", "people": "person", "accounts": "account", "roles": "role", "links": "link",
        }.get(key, "entity:"+key)
        if key == "record:policy":
            actions += [B(label, "catalog", catalog="policy:"+k) for k, label in repo.POLICY_LABELS.items()]
        else:
            actions.append(B("+ Новая запись", "new", kind=kind))
        actions += [B("Найти запись", "search", target="catalog", target_params={"catalog": key}), B("← Настройки", "settings")]
        return show(db, actor, s, f"{title(key)}\nНайдено: {len(rows)}." + (f" Поиск: {search}" if search else ""), actions, "catalog", catalog=key, page=page, search=search)
    if action == "object":
        obj = object_for(db, actor, p["catalog"], p["object_id"])
        # Use an unsaved object to format existing data without creating a draft on each read.
        if isinstance(obj, Record):
            kind = "record:"+obj.kind+(":"+obj.data.get("policy_type", "generic") if obj.kind == "policy" else "")
            data = {"key": obj.key, "owner_id": obj.owner_id, "value": obj.data}
        elif isinstance(obj, Entity):
            kind, data = "entity:"+obj.kind, {"name": obj.name, "partner_id": obj.partner_id, "data": obj.data}
        else:
            kind = next((k for k, v in MODEL_CATALOG.items() if v == p["catalog"]), "person")
            data = {c.name: getattr(obj, c.name) for c in obj.__table__.columns if c.name not in {"id", "bot_id"}}
        text = f"{repo.label(db, obj)}\nID: {obj.id}\nВерсия: {getattr(obj, 'version', 1)}\n" + "\n".join(content_lines(db, kind, data))
        if isinstance(obj, Binding):
            text += f"\nТест доставки: {'пройден' if obj.verified else 'не пройден'}\nТема: {obj.topic_key or 'без темы'}"
        actions = [B("Редактировать…", "edit", **p), B("История изменений", "history", object_id=obj.id)]
        if isinstance(obj, Record) and obj.published:
            from ama.ui.configuration import dependencies
            deps = dependencies(db, obj)
            text += "\nЗависимости: " + ("; ".join(deps[:8]) or "не обнаружены")
            actions.append(B("Архивировать…", "config_action", command="archive_record", **p))
        if isinstance(obj, Binding):
            actions.append(B("Проверить доставку…", "config_action", command="probe_binding", **p))
            if obj.verified and not obj.active:
                actions.append(B("Подключить чат…", "config_action", command="activate_binding", **p))
            if obj.active:
                actions += [B("Заменить чат…", "config_action", command="replace_binding", **p), B("Отключить чат…", "config_action", command="disable_binding", **p)]
        if isinstance(obj, Role) and obj.active:
            actions.append(B("Отозвать назначение…", "config_action", command="revoke_role", **p))
        if isinstance(obj, EstablishmentBrand):
            actions.append(B("Отключить связь…" if obj.active else "Восстановить связь…", "config_action", command="link_update", **p))
        actions += [B("← Список", "catalog", catalog=p["catalog"]), B("← Настройки", "settings")]
        return show(db, actor, s, text, actions, "object", **p)
    if action == "edit":
        obj = object_for(db, actor, p["catalog"], p["object_id"])
        if isinstance(obj, EstablishmentBrand):
            raise BusinessError("Связь меняется отдельным отключением или созданием новой связи.")
        d = business.from_object(db, actor, obj)
        return draft_screen(db, actor, s, {"draft_id": d.id})
    if action == "config_action":
        obj = object_for(db, actor, p["catalog"], p["object_id"])
        command = p["command"]
        data = {"nonce": uid()} if command == "probe_binding" else {"active": not obj.active} if command == "link_update" else {}
        if command == "replace_binding":
            data = {"old_id": obj.id}
        d = config_command(db, actor, obj, command, data)
        return draft_screen(db, actor, s, {"draft_id": d.id})
    if action == "new":
        return draft_screen(db, actor, s, {"draft_id": new_draft(db, actor, p["kind"], p.get("data")).id})
    if action == "draft":
        return draft_screen(db, actor, s, p)
    if action == "field":
        return field_screen(db, actor, s, p)
    if action in {"field_set", "field_clear"}:
        d = view_draft(db, actor, p["draft_id"])
        spec = schema.descriptor(d.kind, d.data, p["path"])
        value = p.get("value")
        if action == "field_clear":
            if spec["required"]:
                raise BusinessError("Обязательное поле нельзя очистить.")
            value = [] if spec["type"] in {"multi", "multiref", "lines", "dates", "integers"} else None
        elif p.get("multi"):
            current = list(schema.value_at(d.data, p["path"], []) or [])
            if value in current:
                current.remove(value)
            else:
                current.append(value)
            value = current
        apply_field(db, actor, d, p["path"], value)
        return field_screen(db, actor, s, p) if p.get("multi") else draft_screen(db, actor, s, {"draft_id": d.id, "path": p["path"][:-1]})
    if action in {"list_add", "list_delete", "list_move"}:
        d = view_draft(db, actor, p["draft_id"])
        path = p["path"]
        parent = path if action == "list_add" else path[:-1]
        spec = schema.descriptor(d.kind, d.data, parent)
        if spec["type"] != "list":
            raise BusinessError("Операция применяется только к списку пунктов.")
        items = deepcopy(schema.value_at(d.data, parent, []) or [])
        if action == "list_add":
            if len(items) >= 500:
                raise BusinessError("Не больше 500 пунктов в одном списке.")
            items.append(defaults(spec.get("fields", [])))
            destination = parent+[len(items)-1]
        elif action == "list_delete":
            del items[int(path[-1])]
            destination = parent
        else:
            i, j = int(path[-1]), int(path[-1])+p["delta"]
            if 0 <= j < len(items):
                items[i], items[j] = items[j], items[i]
            destination = parent
        update(d, schema.set_at(d.data, parent, items))
        return draft_screen(db, actor, s, {"draft_id": d.id, "path": destination})
    if action == "extra_key":
        d = view_draft(db, actor, p["draft_id"])
        if d.kind != "record:policy:generic" or p["path"][:2] != ["value", "settings"]:
            raise AccessDenied("Произвольные поля доступны только в редакторе общих правил.")
        prompt = {"type": "extra_key", "draft_id": d.id, "draft_version": d.version, "path": p["path"], "expires": (now()+timedelta(minutes=15)).isoformat()}
        return render(db, actor, s, "Пришлите постоянный ключ нового параметра латиницей.", [B("Назад", "draft", draft_id=d.id, path=p["path"])], prompt=prompt)
    if action == "extra_type":
        d = view_draft(db, actor, p["draft_id"])
        if d.kind != "record:policy:generic" or p["path"][:2] != ["value", "settings"]:
            raise AccessDenied("Нет доступа к произвольным полям.")
        value = {"text": "", "int": 0, "bool": False, "lines": [], "object": {}, "list": [{}]}[p["kind"]]
        update(d, schema.set_at(d.data, p["path"]+[p["key"]], value))
        return draft_screen(db, actor, s, {"draft_id": d.id, "path": p["path"]})
    if action == "search":
        prompt = {"type": "search", "action": p["target"], "params": p["target_params"], "expires": (now()+timedelta(minutes=15)).isoformat()}
        return render(db, actor, s, "Пришлите название или ID для поиска.", [B("Назад", p["target"], **p["target_params"])], prompt=prompt)
    if action == "draft_save":
        d = view_draft(db, actor, p["draft_id"])
        if d.status not in {"draft", "saved", "checked"}:
            raise BusinessError("Завершённый черновик нельзя сохранить повторно.")
        d.status, d.updated_at = "saved", now()
        return show(db, actor, s, "Черновик сохранён. Продолжить можно через «Мои черновики». Действующие настройки не изменены.", [B("Продолжить редактирование", "draft", draft_id=d.id), B("В меню", "home")])
    if action == "draft_cancel":
        d = view_draft(db, actor, p["draft_id"])
        return show(db, actor, s, "Отменить этот черновик?", [B("Да, отменить черновик", "draft_discard", draft_id=d.id), B("Продолжить редактирование", "draft", draft_id=d.id)])
    if action == "draft_discard":
        d = view_draft(db, actor, p["draft_id"])
        business.authorize(db, actor, d)
        d.status, d.version = "cancelled", d.version+1
        return dispatch(db, actor, s, "drafts", {})
    if action == "drafts":
        query = select(UIDraft).where(UIDraft.bot_id == settings().bot_id, UIDraft.person_id == actor.person_id,
                                    UIDraft.binding_id == actor.binding_id, UIDraft.status.in_(["draft", "saved", "checked"])).order_by(UIDraft.updated_at.desc())
        rows = []
        for d in db.scalars(query):
            try:
                view_draft(db, actor, d.id)
                rows.append(d)
            except AccessDenied:
                continue
        items, page = paginate(rows, p.get("page", 0))
        actions = [B(d.title, "draft", draft_id=d.id) for d in items]
        actions += navigation("drafts", page, len(rows)) + [B("В меню", "home")]
        return show(db, actor, s, f"Мои черновики в этом чате: {len(rows)}.", actions, "drafts", page=page)
    if action == "draft_reload":
        d = view_draft(db, actor, p["draft_id"])
        if not business.configuration(d.kind) or d.kind.startswith("config:"):
            raise BusinessError("Откройте актуальную рабочую карточку из меню. Сохранённый черновик не перезаписан.")
        key = d.kind if d.kind.startswith("record:") else MODEL_CATALOG.get(d.kind) or d.kind.split(":")[-1]
        if d.kind.startswith("record:policy:"):
            key = "policy:"+d.kind.split(":")[2]
        obj = object_for(db, actor, key, d.target_id)
        fresh = business.from_object(db, actor, obj)
        return draft_screen(db, actor, s, {"draft_id": fresh.id})
    if action == "preview":
        d = view_draft(db, actor, p["draft_id"])
        result = business.preview(db, actor, d, publish=p.get("publish", False))
        d.status = "checked"
        d.before = {**d.before, "_checked": {"grants": access.grants_hash(db, actor),
                    "recipients": result.get("recipients"), "audience_versions": result.get("audience_versions"),
                    "reply_contact":result.get("reply_contact"), "reply_epoch":result.get("reply_epoch"), "case_versions":result.get("case_versions"),"packet_fingerprint":result.get("fingerprint"),"packet_audience":result.get("audience")}}
        text = f"Предпросмотр: {d.title}\n" + "\n".join(content_lines(db, d.kind, d.data))
        differences = values.structural_diff({k: v for k, v in d.before.items() if not k.startswith("_")}, d.data)
        if differences:
            text += "\nИзменённые поля: " + str(len(differences))
            for key, before, after in differences[:15]:
                path = [int(v) if v.isdigit() else v for v in key.split(".")]
                try:
                    spec = schema.descriptor(d.kind, d.data, path)
                except (IndexError, TypeError):
                    spec = schema.infer(key, after)
                text += f"\n{spec['label']}: {field_label(db, spec, before)[:160]} → {field_label(db, spec, after)[:200]}"
        if result.get("audience"):
            snap=result["audience"]
            text+=f"\nАудитория: {len({i['binding_id'] for i in snap['included']})} чатов, {len({i['partner_id'] for i in snap['included']})} ФП, {len({i['establishment_id'] for i in snap['included']})} предприятий; исключений: {len(snap['excluded'])}."
        text += f"\nУведомлений: {result.get('notifications', 0)}."
        if result.get("destinations"):
            text += "\nПолучатели: " + "; ".join(repo.label(db, db.get(Binding, r["binding_id"])) if r.get("binding_id") else f"{r['platform']} · {r['chat_id']}" for r in result["destinations"][:20])
        if "recipients" in result:
            text += f"\nБрендовых чатов: {len(result['recipients'])}."
        if "works" in result:
            text += f"\nПунктов проекта: {len(result['works']) if isinstance(result['works'],list) else result['works']}."
        if "contract_amount_minor" in result:
            text += "\nДоговорный пакет: "+values.display(result["contract_amount_minor"],{"type":"money"})+". Подтверждённых интересов: "+str(len(result["case_versions"]))
        if "rows" in result:
            text += f"\nСтрок отчёта: {result['rows']}.\nПолучатель: {result['recipient']}"
        if d.target_id and d.kind.startswith("record:"):
            from ama.ui.configuration import dependencies
            deps = dependencies(db, db.get(Record, d.target_id))
            text += "\nСвязанные процессы: "+("; ".join(deps[:10]) or "не найдены")
            text += "\nПубликация обновит справочник. Сроки открытых работ и персональные подтверждения сохраняются."
        if d.kind in {"record:policy:sla", "record:policy:scenario", "record:policy:schedule"}:
            text += "\nПубликуется редакция правила. Создание работ выполняется отдельно в составе проекта; расписания и расширенные SLA подключаются на следующих этапах реализации."
        # Full public content preview, including individual course cards, is accessible before applying.
        if d.kind.startswith("record:"):
            text += "\nРежим: " + ("публикация" if p.get("publish") else "сохранение неопубликованной записи")
        actions = [B("Подтвердить применение", "confirm", draft_id=d.id, expected=d.version, publish=p.get("publish", False)),
                   B("Полный текст и поля", "content_preview", draft_id=d.id, publish=p.get("publish", False)), B("Вернуться к редактированию", "draft", draft_id=d.id), B("В меню", "home")]
        if result.get("audience"):
            actions.insert(1,B("Проверить включённые предприятия и исключения","pk_preview_audience",draft_id=d.id))
        return show(db, actor, s, text + "\nИзменения ещё не применены.", actions, "draft", draft_id=d.id)
    if action == "content_preview":
        d = view_draft(db, actor, p["draft_id"])
        lines = []
        def walk(data, path):
            spec = schema.descriptor(d.kind, d.data, path)
            if isinstance(data, dict):
                if path:
                    lines.append("\n"+spec["label"])
                for key, value in data.items():
                    if key not in {"command", "_checked", "policy_type"}:
                        walk(value, path+[key])
            elif isinstance(data, list) and spec["type"] == "list":
                for i, value in enumerate(data):
                    walk(value, path+[i])
            else:
                lines.append(spec["label"]+": "+field_label(db, spec, data))
        walk(d.data, [])
        # Paginate complete field texts, while send() splits long messages without cutting data.
        items, page = paginate(lines, p.get("page", 0))
        return show(db, actor, s, d.title+"\n"+"\n".join(items), navigation("content_preview", page, len(lines), draft_id=d.id)+
                    [B("К предпросмотру", "preview", draft_id=d.id, publish=p.get("publish", False)), B("К форме", "draft", draft_id=d.id)], "content_preview", **p)
    if action == "confirm":
        d = view_draft(db, actor, p["draft_id"])
        result = business.confirm(db, actor, d, p["expected"], publish=p.get("publish", False))
        actions = [B("В меню", "home"), B("Мои черновики", "drafts")]
        if d.kind.startswith("case:"):
            actions.insert(0, B("Открыть интерес", "sales_case", case_id=d.target_id))
        if d.kind.startswith("expert:"):
            actions.insert(0, B("Открыть консультацию", "expert", expert_id=d.target_id))
        if d.kind.startswith("work:") or d.kind == "create_work" or d.kind.startswith("operation:"):
            actions.insert(0, B("Открыть работу", "work", work_id=result["id"]))
        return show(db, actor, s, "Подтверждено: " + d.title + ("\nID: " + result["id"] if result.get("id") else "") + "\nУведомления переданы в очередь доставки.", actions)
    if action in {"history", "revision", "rollback"}:
        admin.check_admin(db, actor)
        if action == "history":
            q = select(ConfigurationRevision).order_by(ConfigurationRevision.created_at.desc(), ConfigurationRevision.id)
            if p.get("object_id"):
                q = q.where(ConfigurationRevision.object_id == p["object_id"])
            rows = list(db.scalars(q))
            items, page = paginate(rows, p.get("page", 0))
            actions = [B(f"{r.created_at:%d.%m %H:%M} UTC · {r.kind} · v{r.new_version}", "revision", revision_id=r.id) for r in items]
            actions += navigation("history", page, len(rows), **({"object_id": p["object_id"]} if p.get("object_id") else {}))
            return show(db, actor, s, f"История настроек: {len(rows)} изменений.", actions+[B("← Настройки", "settings")], "history", **p)
        r = db.get(ConfigurationRevision, p["revision_id"])
        if not r:
            raise BusinessError("Редакция не найдена.")
        if action == "rollback":
            if r.kind != "record":
                raise BusinessError("Восстановление доступно для версий содержимого. Аккаунты, платежи и события истории не откатываются.")
            obj = db.get(Record, r.object_id)
            if not obj:
                raise BusinessError("Исходная запись отсутствует.")
            d = business.from_record(db, actor, obj)
            update(d, {**d.data, "value": deepcopy(r.after["data"]), "reason": "Восстановление содержимого версии " + str(r.new_version)})
            return draft_screen(db, actor, s, {"draft_id": d.id})
        text = f"{r.kind} · {r.object_id}\nВерсия: {r.old_version} → {r.new_version}\nАвтор: {repo.name(db, r.actor_id)}\nОснование: {r.reason}\n"
        for key, old, new in values.structural_diff(r.before, r.after)[:30]:
            text += f"\n{key}: {values.display(old)[:180]} → {values.display(new)[:180]}"
        actions = [B("История записи", "history", object_id=r.object_id)]
        if r.kind == "record":
            actions.insert(0, B("Восстановить эту редакцию через черновик…", "rollback", revision_id=r.id))
        return show(db, actor, s, text, actions+[B("← Настройки", "settings")], "revision", **p)
    if action in {"diagnostics", "diagnostic_item", "diagnostic_retry", "diagnostic_reconcile", "check"}:
        return diagnostics(db, actor, s, action, p)
    from ama.ui.workspace import dispatch as workspace
    return workspace(db, actor, s, action, p)


def diagnostics(db, actor, s, action, p):
    admin.check_admin(db, actor)
    models = {"delivery": Outbox, "inbox": Inbox, "attachment": Attachment, "timer": Timer}
    if action == "check":
        from ama.ui.configuration import validate_record
        problems = []
        for r in db.scalars(select(Record).where(Record.published.is_(True))):
            try:
                validate_record(db, r.kind, r.data, published=True)
                if not r.owner_id:
                    problems.append(f"{r.key}: не указан владелец содержимого")
            except BusinessError as error:
                problems.append(f"{r.key}: {error}")
        if not db.scalar(select(Binding).where(Binding.bot_id == settings().bot_id, Binding.purpose == "support", Binding.active.is_(True), Binding.verified.is_(True))):
            problems.append("Нет действующего проверенного чата сопровождения")
        return show(db, actor, s, "Проверка опубликованных настроек:\n" + ("\n".join(problems) or "Нарушений не найдено."), [B("← Настройки", "settings")], "check")
    if action == "diagnostics":
        key = p.get("queue", "delivery")
        model = models[key]
        states = ["failed", "blocked", "uncertain", "sending"] if key == "delivery" else ["failed", "rejected"] if key == "inbox" else ["failed"] if key == "attachment" else ["blocked"]
        rows = list(db.scalars(select(model).where(model.status.in_(states)).order_by(model.key if model is Inbox else model.id)))
        items, page = paginate(rows, p.get("page", 0))
        actions = [B(f"{r.status} · {(r.key if isinstance(r, Inbox) else r.id)[:12]}", "diagnostic_item", queue=key, object_id=r.key if isinstance(r, Inbox) else r.id) for r in items]
        actions += navigation("diagnostics", page, len(rows), queue=key)
        actions += [B(label, "diagnostics", queue=k) for k, label in {"delivery": "Доставка", "inbox": "Входящие", "attachment": "Файлы", "timer": "Контроль сроков"}.items() if k != key]
        actions += [B("Выгрузить журнал настроек…", "new", kind="export", data={"report": "audit"}), B("← Настройки", "settings")]
        return show(db, actor, s, f"Диагностика · {key}\nТребуют внимания: {len(rows)}.\nСодержимое сообщений и токены в журнале не показываются.", actions, "diagnostics", queue=key, page=page)
    obj = db.get(models[p["queue"]], p["object_id"])
    if not obj:
        raise BusinessError("Запись очереди отсутствует.")
    if action in {"diagnostic_retry", "diagnostic_reconcile"}:
        if action == "diagnostic_reconcile" and p["queue"] != "delivery":
            raise BusinessError("Сверка предназначена только для доставки.")
        d = config_command(db, actor, obj, "reconcile_delivery" if action == "diagnostic_reconcile" else "retry_"+p["queue"], {"expected_status": obj.status})
        return draft_screen(db, actor, s, {"draft_id": d.id})
    actions = [B("Повторить после устранения причины…", "diagnostic_retry", **p)] if obj.status not in {"sending", "uncertain"} else [B("Зафиксировать проверенный исход…", "diagnostic_reconcile", **p)]
    text = f"{obj.key if isinstance(obj, Inbox) else obj.id}\nСостояние: {obj.status}\nПопыток: {getattr(obj, 'attempts', '—')}"
    if obj.status in {"sending", "uncertain"}:
        text += "\nИсход доставки неизвестен. Проверьте наличие сообщения в чате. Откройте форму сверки только после проверки фактического результата. Для зависшей sending сначала остановите все воркеры и дождитесь завершения отправок, затем запустите один для обработки сверки."
    return show(db, actor, s, text, actions+[B("← Диагностика", "diagnostics", queue=p["queue"])], "diagnostic_item", **p)
