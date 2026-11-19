"""Durable actor-bound screens and optimistic callbacks under 64 bytes."""
from copy import deepcopy
from datetime import datetime
from sqlalchemy import select
from ama.config import settings
from ama.errors import BusinessError, AccessDenied, Conflict
from ama.models import UIScreen, UIDraft, Binding, Outbox, Work, Record, Entity, Lead, Deal, now
from ama.service import versioned
from ama.ui import access, business, schema, values

PAGE_SIZE = 6


def button(label, screen_action, **params):
    return {"label": label, "action": screen_action, "params": params}


def session(db, actor):
    access.assignments(db, actor)
    b = db.get(Binding, actor.binding_id)
    s = db.scalar(select(UIScreen).where(UIScreen.bot_id == settings().bot_id, UIScreen.person_id == actor.person_id,
                                        UIScreen.binding_id == actor.binding_id).with_for_update())
    if not s:
        s = UIScreen(bot_id=settings().bot_id, person_id=actor.person_id, binding_id=actor.binding_id,
                     binding_version=b.version, state={}, version=1)
        db.add(s)
        db.flush()
    if s.binding_version != b.version:
        s.binding_version, s.state, s.version = b.version, {}, s.version + 1
    return s


def render(db, actor, s, text, actions, *, view=None, prompt=None):
    s.version += 1
    s.updated_at = now()
    s.state = {"choices": actions, "view": view or {"action": "home", "params": {}}, "prompt": prompt}
    return text, [[a["label"][:70], f"af:ui:{s.id}:{s.version}:{i}"] for i, a in enumerate(actions)]


def view_draft(db, actor, draft_id):
    d = db.scalar(select(UIDraft).where(UIDraft.id == draft_id).with_for_update())
    if not d or d.person_id != actor.person_id or d.binding_id != actor.binding_id or d.bot_id != settings().bot_id:
        raise AccessDenied("Черновик принадлежит другому человеку или чату.")
    if d.kind.startswith(("finance:","learning:","digital:","packet:","obligation:")):
        business.authorize(db,actor,d)
    elif d.kind == "client_sla:return":
        business.authorize(db,actor,d)
    elif d.kind.startswith("signal:"):
        business.authorize(db,actor,d)
    elif d.kind.startswith("plan:"):
        if d.kind == "plan:create":
            access.require_staff(db, actor)
        else:
            business.authorize(db, actor, d)
    elif d.kind.startswith(("case:", "expert:")):
        from ama import sale_flow
        from ama.models import SalesExpert
        if d.kind.startswith("case:"):
            sale_flow.authorize(db, actor, sale_flow.get(db, d.target_id))
        else:
            expert = db.get(SalesExpert, d.target_id)
            if not expert or expert.owner_id != actor.person_id or expert.binding_id != actor.binding_id or not access.staff_role(db, actor, "chief_marketer"):
                raise AccessDenied("Внутренняя консультация недоступна.")
    elif business.configuration(d.kind) or d.kind == "export" and d.data.get("report") == "audit":
        from ama.admin import check_admin
        check_admin(db, actor)
    elif d.kind.startswith(("work:", "operation:")):
        w = db.get(Work, d.target_id)
        if not w or not access.can_work(db, actor, w, detail=True):
            raise AccessDenied("Больше нет доступа к исходной работе.")
    else:
        access.require_staff(db, actor)
        if d.kind in {"create_work", "project"}:
            if not any(r.name in {"curator", "support_head", "marketing_manager"} and access.staff_role(db, actor, r.name)
                       and all(not d.data.get(k) or not getattr(r, k) or d.data[k] == getattr(r, k)
                               for k in ("partner_id", "brand_id", "establishment_id")) for r in access.assignments(db, actor)):
                raise AccessDenied("Область черновика больше не соответствует назначению.")
        if d.kind.startswith(("sales:", "contract:", "deal:")):
            target = db.get(Deal if d.kind.startswith("deal:") else Lead, d.target_id)
            if not target or not (access.can_deal(db, actor, target) if d.kind.startswith("deal:") else access.can_lead(db, actor, target)):
                raise AccessDenied("Нет доступа к исходной продаже.")
    return d


def update(draft, data):
    draft.data, draft.version, draft.status, draft.updated_at = data, draft.version + 1, "draft", now()


def apply_field(db, actor, draft, path, value):
    spec = schema.descriptor(draft.kind, draft.data, path)
    immutable = {"record": {"key"}, "account": {"person_id", "platform", "external_id"}, "entity": {"partner_id"},
                 "role": {"person_id"}, "link": {"establishment_id", "brand_id"}}
    if draft.target_id and len(path) == 1 and path[0] in immutable.get(draft.kind.split(":")[0], set()):
        raise BusinessError("Это постоянная связь. Используйте отдельную запись или проверенную замену.")
    if spec["type"] in {"ref", "multiref"}:
        from ama.ui.repository import refs
        permitted = {key for key, _ in refs(db, actor, spec["source"], draft)}
        chosen = value if isinstance(value, list) else [value] if value is not None else []
        if any(item not in permitted for item in chosen):
            raise AccessDenied("Вариант больше недоступен в вашей области. Обновите список.")
    data = schema.set_at(draft.data, path, value)
    if path == ["partner_id"] and data.get("establishment_id"):
        est = db.get(Entity, data["establishment_id"])
        if est and est.partner_id != value:
            data["establishment_id"] = None
    if path == ["establishment_id"] and value:
        est = db.get(Entity, value)
        if est:
            data["partner_id"] = est.partner_id
    if path == ["commercial_key"]:
        rec = db.scalar(select(Record).where(Record.kind == "commercial", Record.key == value))
        data["commercial_version"] = rec.version if rec else None
    update(draft, data)


def dispatch(db, actor, s, action, params=None):
    from ama.ui.controller import dispatch as controller
    return controller(db, actor, s, action, params or {})


def callback(db, actor, payload):
    p = payload.split(":")
    if len(p) != 5:
        raise BusinessError("Повреждённая кнопка. Откройте меню заново.")
    try:
        expected, index = int(p[3]), int(p[4])
    except ValueError as error:
        raise BusinessError("Повреждённая кнопка.") from error
    s = db.scalar(select(UIScreen).where(UIScreen.id == p[2]).with_for_update())
    if not s or (s.person_id, s.binding_id, s.bot_id) != (actor.person_id, actor.binding_id, settings().bot_id):
        raise AccessDenied("Это меню другого сотрудника или чата. Откройте своё командой /start.")
    access.assignments(db, actor)
    if s.binding_version != db.get(Binding, actor.binding_id).version:
        raise Conflict("Маршрут изменился. Откройте меню заново.")
    versioned(s, expected)
    choices = s.state.get("choices", [])
    if not 0 <= index < len(choices):
        raise BusinessError("Кнопка отсутствует на этом экране.")
    selected = deepcopy(choices[index])
    previous = deepcopy(s.state.get("view", {"action": "home", "params": {}}))
    try:
        with db.begin_nested():
            return dispatch(db, actor, s, selected["action"], selected.get("params", {}))
    except AccessDenied:
        raise
    except Conflict as error:
        actions = [button("В меню", "home")]
        draft_id = selected.get("params", {}).get("draft_id")
        if draft_id:
            d = view_draft(db, actor, draft_id)
            actions.insert(0, button("Открыть сохранённый черновик", "draft", draft_id=d.id))
            if d.target_id:
                actions.insert(0, button("Перечитать актуальную запись", "draft_reload", draft_id=d.id))
        return render(db, actor, s, str(error) + "\nУстаревшие данные не опубликованы. Черновик сохранён.", actions)
    except BusinessError as error:
        return render(db, actor, s, str(error), [button("Вернуться", previous["action"], **previous.get("params", {})), button("В меню", "home")])


def input_text(db, actor, text):
    s = db.scalar(select(UIScreen).where(UIScreen.bot_id == settings().bot_id, UIScreen.person_id == actor.person_id,
                                        UIScreen.binding_id == actor.binding_id).with_for_update())
    if not s or not s.state.get("prompt"):
        return None
    access.assignments(db, actor)
    if s.binding_version != db.get(Binding, actor.binding_id).version:
        raise AccessDenied("Маршрут изменён. Откройте форму заново.")
    prompt = deepcopy(s.state["prompt"])
    if prompt.get("expires") and datetime.fromisoformat(prompt["expires"]) < now():
        return render(db, actor, s, "Время ввода истекло. Черновик сохранён.", [button("Мои черновики", "drafts"), button("В меню", "home")])
    if prompt["type"] == "attachment":
        return None
    try:
        with db.begin_nested():
            if prompt["type"] == "search":
                return dispatch(db, actor, s, prompt["action"], {**prompt["params"], "search": text.strip()[:200], "page": 0})
            d = view_draft(db, actor, prompt["draft_id"])
            versioned(d, prompt["draft_version"])
            if prompt["type"] == "extra_key":
                key = values.parse(text, {"type": "key"})
                from ama.ui.configuration import SECRET_KEYS
                if key.lower() in SECRET_KEYS or key in schema.value_at(d.data, prompt["path"], {}):
                    raise BusinessError("Поле уже существует или содержит секрет.")
                return render(db, actor, s, "Тип нового параметра", [button(label, "extra_type", draft_id=d.id, path=prompt["path"], key=key, kind=kind)
                    for kind, label in {"text": "Текст", "int": "Число", "bool": "Да / Нет", "lines": "Список строк", "object": "Группа полей", "list": "Список групп"}.items()])
            field = schema.descriptor(d.kind, d.data, prompt["path"])
            timezone = "Europe/Moscow"
            est = db.get(Entity, d.data.get("establishment_id")) if d.data.get("establishment_id") else None
            if est:
                timezone = est.data.get("timezone") or timezone
            apply_field(db, actor, d, prompt["path"], values.parse(text, field, timezone))
            return dispatch(db, actor, s, "draft", {"draft_id": d.id, "path": prompt["path"][:-1]})
    except AccessDenied:
        raise
    except Conflict as error:
        return render(db, actor, s, str(error), [button("Открыть черновик", "draft", draft_id=prompt["draft_id"]), button("В меню", "home")])
    except BusinessError as error:
        return render(db, actor, s, str(error) + "\nПопробуйте ещё раз или вернитесь к форме.",
                      [button("Назад", "draft", draft_id=prompt.get("draft_id"), path=prompt.get("path", [])[:-1]), button("В меню", "home")], prompt=prompt)


def attachment_context(db, actor):
    s = db.scalar(select(UIScreen).where(UIScreen.bot_id == settings().bot_id, UIScreen.person_id == actor.person_id,
                                        UIScreen.binding_id == actor.binding_id).with_for_update())
    prompt = s.state.get("prompt", {}) if s else {}
    if not prompt or prompt.get("type") != "attachment":
        return None
    if s.binding_version != db.get(Binding, actor.binding_id).version or datetime.fromisoformat(prompt["expires"]) < now():
        raise BusinessError("Контекст загрузки истёк. Откройте пункт и нажмите «Добавить файл» ещё раз.")
    w = db.get(Work, prompt["work_id"])
    if not w or not access.can_work(db, actor, w, detail=True):
        raise AccessDenied("Нет доступа к выбранному пункту.")
    versioned(w, prompt["work_version"])
    return w.id


def reply(db, actor, event, response):
    from ama.bot import send
    grants = access.grants_hash(db, actor)
    if event.callback_id:
        db.add(Outbox(dedupe_key="ui-callback:" + event.key, platform=event.platform, chat_id=event.chat_id, topic_key=event.topic,
                      payload={"callback_id": event.callback_id}))
    send(db, event, *response)
    for obj in list(db.new):
        if isinstance(obj, Outbox) and obj.dedupe_key.startswith("reply:" + event.key + ":"):
            obj.payload = {**obj.payload, "ui_actor": actor.person_id, "ui_grants": grants}
