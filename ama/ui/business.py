"""Draft validation, sandboxed previews and confirmed calls to domain services."""
from copy import deepcopy
from types import SimpleNamespace
from sqlalchemy import select, func
from ama import admin, sales, projects, broadcasts, operations
from ama.config import settings
from ama.errors import BusinessError, AccessDenied, Conflict
from ama.models import (Work, Record, Entity, Identity, Account, Role, Binding, EstablishmentBrand, Lead, Deal,
                        Outbox, Audit, UIDraft, now)
from ama.service import create_work, transition, versioned
from ama.ui import access, schema
from ama.ui.configuration import validate_scope
from ama.ui.values import required_errors

CONFIG_KINDS = {"person", "account", "role", "employee", "binding", "link", "substitution"}


def configuration(kind):
    return kind in CONFIG_KINDS or kind.startswith(("record:", "entity:", "config:"))


def authorize(db, actor, draft):
    if draft.bot_id != settings().bot_id or draft.person_id != actor.person_id or draft.binding_id != actor.binding_id:
        raise AccessDenied("Черновик принадлежит другому пользователю, боту или чату.")
    if draft.status not in {"draft", "checked", "saved"}:
        raise BusinessError("Черновик уже применён или отменён. Откройте актуальную запись.")
    if draft.kind.startswith(("case:", "expert:")):
        from ama import sale_flow
        from ama.models import SalesExpert
        if draft.kind.startswith("case:"):
            obj = sale_flow.get(db, draft.target_id)
            sale_flow.authorize(db, actor, obj)
        else:
            obj = db.get(SalesExpert, draft.target_id)
            if not obj or obj.owner_id != actor.person_id or obj.binding_id != actor.binding_id:
                raise AccessDenied("Нет доступа к внутренней консультации.")
            case = sale_flow.get(db, obj.case_id)
            if not access.scope(db, actor, SimpleNamespace(partner_id=None, brand_id=case.brand_id, establishment_id=None), {"chief_marketer"}):
                raise AccessDenied("Нет назначения Главного маркетолога.")
        versioned(obj, draft.target_version)
        return
    if configuration(draft.kind):
        admin.check_admin(db, actor)
        return
    if draft.kind.startswith(("work:", "operation:")):
        w = db.get(Work, draft.target_id)
        if not w or not access.can_work(db, actor, w, detail=True):
            raise AccessDenied("Работа недоступна в этом чате / теме.")
        versioned(w, draft.target_version)
        from ama.ui.workspace import work_actions, operations as available_operations
        permitted = work_actions(db, actor, w) if draft.kind.startswith("work:") else available_operations(db, actor, w)
        if draft.kind.split(":")[1] not in permitted:
            raise AccessDenied("Действие недоступно текущей роли в этом чате и состоянии работы.")
        return
    if draft.kind == "export" and draft.data.get("report") == "audit":
        admin.check_admin(db, actor)
        return
    access.require_staff(db, actor)
    if draft.kind == "create_work" or draft.kind == "project":
        d = draft.data
        if not access.scope(db, actor, SimpleNamespace(partner_id=d.get("partner_id"), brand_id=d.get("brand_id"), establishment_id=d.get("establishment_id")),
                            {"curator", "support_head", "marketing_manager"}):
            raise AccessDenied("Нет назначения в области выбранного ФП, бренда и предприятия.")
    if draft.kind == "create_work":
        obj = SimpleNamespace(partner_id=draft.data.get("partner_id"), brand_id=draft.data.get("brand_id"), establishment_id=draft.data.get("establishment_id"))
        if not access.scope(db, actor, obj, {"curator", "support_head"}):
            work_kind = draft.data.get("kind", "")
            if not (work_kind.startswith("marketing") or work_kind in {"design", "smm", "advertising", "printing"}):
                raise AccessDenied("Менеджер маркетинга создаёт только маркетинговые работы в своей области.")
    if draft.kind.startswith(("sales:", "contract:")):
        lead = db.get(Lead, draft.target_id)
        if not lead or not access.can_lead(db, actor, lead):
            raise AccessDenied("Лид назначен другому консультанту или находится вне вашей области.")
        versioned(lead, draft.target_version)
    if draft.kind.startswith("deal:"):
        deal = db.get(Deal, draft.target_id)
        if not deal or not access.can_deal(db, actor, deal):
            raise AccessDenied("Сделка недоступна в вашем рабочем контуре.")
        versioned(deal, draft.target_version)


def make_draft(db, actor, kind, title, data=None, target=None, before=None):
    draft = UIDraft(bot_id=settings().bot_id, person_id=actor.person_id, binding_id=actor.binding_id, kind=kind,
                    title=title[:250], data=deepcopy(data or {}), before=deepcopy(before or data or {}),
                    target_id=target.id if target else None, target_version=getattr(target, "version", None), version=1)
    db.add(draft)
    db.flush()
    return draft


def from_record(db, actor, obj):
    data = {"key": obj.key, "owner_id": obj.owner_id, "value": deepcopy(obj.data), "reason": ""}
    kind = "record:" + obj.kind + (":" + obj.data.get("policy_type", "generic") if obj.kind == "policy" else "")
    return make_draft(db, actor, kind, obj.data.get("title") or obj.key, data, obj, data)


def from_object(db, actor, obj):
    if isinstance(obj, Record):
        return from_record(db, actor, obj)
    if isinstance(obj, Entity):
        kind = "entity:" + obj.kind
        data = {"name": obj.name, "partner_id": obj.partner_id, "data": deepcopy(obj.data), "reason": ""}
    elif isinstance(obj, Identity):
        kind, data = "person", {"name": obj.name, "reason": ""}
    elif isinstance(obj, Account):
        kind, data = "account", {"person_id": obj.person_id, "platform": obj.platform, "external_id": obj.external_id, "active": obj.active, "reason": ""}
    elif isinstance(obj, Role):
        kind, data = "role", {"person_id": obj.person_id, "name": obj.name, "partner_id": obj.partner_id, "brand_id": obj.brand_id,
                              "establishment_id": obj.establishment_id, "expires_at": obj.expires_at.isoformat() + "+00:00" if obj.expires_at else None, "reason": ""}
    elif isinstance(obj, Binding):
        kind, data = "binding", {k: getattr(obj, k) for k in ("platform", "chat_id", "topic_key", "purpose", "partner_id", "brand_id", "establishment_id")}
        data["reason"] = ""
    elif isinstance(obj, EstablishmentBrand):
        kind, data = "link", {"establishment_id": obj.establishment_id, "brand_id": obj.brand_id, "reason": ""}
    else:
        raise BusinessError("Эта запись редактируется в профильной карточке.")
    from ama.ui.repository import label
    return make_draft(db, actor, kind, label(db, obj), data, obj, data)


def execute(db, actor, draft, *, publish=False):
    authorize(db, actor, draft)
    kind, d = draft.kind, deepcopy(draft.data)
    if kind.startswith("case:"):
        from ama.sale_flow import action
        return action(db, actor, draft.target_id, draft.target_version, kind.split(":")[1], d, "ui:"+draft.id)
    if kind.startswith("expert:"):
        from ama.sale_flow import expert_action
        return expert_action(db, actor, draft.target_id, draft.target_version, kind.split(":")[1], d)
    if kind.startswith("config:"):
        command = d["command"]
        models = {"probe_binding": Binding, "activate_binding": Binding, "replace_binding": Binding,
                  "disable_binding": Binding, "revoke_role": Role, "archive_record": Record, "link_update": EstablishmentBrand}
        if command["type"] in models:
            obj = db.scalar(select(models[command["type"]]).where(models[command["type"]].id == command["data"]["id"]).with_for_update())
            if not obj:
                raise BusinessError("Исходная запись отсутствует.")
            versioned(obj, command["data"]["version"])
        command["data"] = {**command["data"], **{k: v for k, v in d.items() if k != "command"},
                           "reason": d.get("reason", "Подтверждённое действие в настройках")}
        return admin.apply(db, actor, command)
    if configuration(kind):
        if draft.target_id:
            model = {"person": Identity, "account": Account, "role": Role, "binding": Binding, "link": EstablishmentBrand,
                     "record": Record, "entity": Entity}[kind.split(":")[0]]
            obj = db.get(model, draft.target_id)
            if not obj:
                raise BusinessError("Редактируемая запись больше не существует.")
            versioned(obj, draft.target_version)
        if kind.startswith("record:"):
            record_kind = kind.split(":")[1]
            if not draft.target_id and db.scalar(select(Record).where(Record.kind == record_kind, Record.key == d["key"])):
                raise Conflict("Запись с этим ключом уже создана. Откройте её вместо создания второй.")
            if draft.target_id and d["key"] != obj.key:
                raise BusinessError("Постоянный ключ существующей записи менять нельзя.")
            if len(kind.split(":")) > 2:
                d["value"]["policy_type"] = kind.split(":")[2]
            return admin.apply(db, actor, {"type": "record", "data": {
                **d, "kind": record_kind, "published": publish,
                **({"version": draft.target_version} if draft.target_id else {})}})
        if kind.startswith("entity:"):
            if draft.target_id:
                if d.get("partner_id") != obj.partner_id:
                    raise BusinessError("Передача предприятия другому ФП требует отдельной проверки связей.")
                return admin.apply(db, actor, {"type": "entity_update", "data": {"id": obj.id, "version": draft.target_version,
                                                  "name": d["name"], "value": d.get("data", {}), "reason": d["reason"]}})
            return admin.apply(db, actor, {"type": "entity", "data": {**d, "kind": kind.split(":")[1]}})
        if kind == "link":
            if draft.target_id:
                raise BusinessError("Связь не переписывается на другую площадку. Создайте отдельную связь.")
            return admin.apply(db, actor, {"type": "link_brand", "data": d})
        command_kind = kind + "_update" if draft.target_id else kind
        if draft.target_id:
            d.update(id=draft.target_id, version=draft.target_version)
        return admin.apply(db, actor, {"type": command_kind, "data": d})
    if kind == "create_work":
        obj = create_work(db, actor, d, "ui:" + draft.id)
        return {"id": obj.id, "version": obj.version, "title": obj.title}
    if kind.startswith("work:"):
        obj = transition(db, actor, draft.target_id, draft.target_version, kind.split(":")[1], d, "ui:" + draft.id)
        return {"id": obj.id, "version": obj.version, "title": obj.title}
    if kind == "project":
        d["project_id"] = draft.id
        validate_scope(db, d)
        if d.get("scenario") == "existing" and not d.get("blocks"):
            return {"works": [], "scenario": "existing", "message": "Только сопровождение, без фиктивного открытия."}
        d["confirmed"] = True
        return projects.configure(db, actor, d, "ui:" + draft.id)
    if kind.startswith("operation:"):
        obj = operations.operation(db, actor, draft.target_id, draft.target_version, kind.split(":")[1], d)
        return {"id": obj.id, "version": obj.version}
    if kind.startswith("sales:"):
        action = kind.split(":")[1]
        if action == "reply":
            lead = db.get(Lead, draft.target_id)
            if lead.owner_id != actor.person_id or lead.no_contact:
                raise AccessDenied("Нет права ответа лиду или установлен запрет контактов.")
            contact = lead.data.get("contact")
            if not contact:
                raise BusinessError("Нет подтверждённого чата лида.")
            row = Outbox(dedupe_key="lead-reply:ui:" + draft.id, platform=contact["platform"], chat_id=contact["chat_id"],
                         payload={"text": d["text"], "lead_id": lead.id, "touch_reason": "consultant_reply",
                                  "silence_epoch": lead.data.get("silence", {}).get("epoch")})
            db.add(row)
            db.flush()
            return {"id": row.id, "recipient": "Подтверждённый личный чат лида", "text": d["text"]}
        obj = sales.update_lead(db, actor, draft.target_id, draft.target_version, action, d)
        return {"id": obj.id, "version": obj.version}
    if kind == "contract:start":
        obj = sales.start_contract(db, actor, draft.target_id, draft.target_version, {"confirm": True})
        return {"id": obj.id, "version": obj.version}
    if kind.startswith("deal:"):
        obj = sales.contract_action(db, actor, draft.target_id, draft.target_version, kind.split(":")[1], d, "ui:" + draft.id)
        return {"id": obj.id, "version": obj.version}
    if kind == "broadcast":
        obj = broadcasts.preview(db, actor, d["text"], d.get("brand_id"))
        broadcasts.publish(db, actor, obj.id, obj.version)
        return {"id": obj.id, "recipients": obj.binding_ids, "audience_versions": obj.audience_versions, "text": obj.text}
    if kind == "export":
        from ama.ui.reports import create_export
        return create_export(db, actor, d, "ui:" + draft.id)
    raise BusinessError("Для этой операции не зарегистрирован исполнитель формы.")


def preview(db, actor, draft, *, publish=False):
    errors = required_errors(schema.fields_for(draft.kind), draft.data)
    if errors:
        raise BusinessError("Заполните обязательные поля: " + "; ".join(errors[:10]))
    authorize(db, actor, draft)
    from ama.ui.configuration import safe_values
    safe_values(draft.data)
    before = db.scalar(select(func.count()).select_from(Outbox))
    transaction = db.begin_nested()
    try:
        result = deepcopy(execute(db, actor, draft, publish=publish))
        db.flush()
        after = db.scalar(select(func.count()).select_from(Outbox))
        messages = [{"chat_id": r.chat_id, "platform": r.platform, "binding_id": r.binding_id} for r in db.scalars(
            select(Outbox).order_by(Outbox.created_at.desc()).limit(max(0, after-before)))] if after > before else []
        result = {**result, "notifications": after - before, "destinations": messages}
    finally:
        transaction.rollback()
    return result


def confirm(db, actor, draft, expected, *, publish=False):
    authorize(db, actor, draft)
    versioned(draft, expected)
    if draft.status != "checked":
        raise BusinessError("Сначала проверьте форму и откройте предпросмотр.")
    checked = draft.before.get("_checked", {})
    if checked.get("grants") != access.grants_hash(db, actor):
        raise Conflict("Назначения изменились после предпросмотра. Проверьте форму заново.")
    errors = required_errors(schema.fields_for(draft.kind), draft.data)
    if errors:
        raise BusinessError("Заполните обязательные поля: " + "; ".join(errors[:10]))
    if draft.kind == "case:contract":
        from ama.sale_flow import get
        for case_id, expected_version in (checked.get("case_versions") or {}).items():
            versioned(get(db,case_id,True),expected_version)
    if draft.kind == "case:reply":
        from ama.sale_flow import get
        case = get(db,draft.target_id)
        lead = db.get(Lead,case.lead_id)
        if lead.data.get("contact") != checked.get("reply_contact") or lead.data.get("silence",{}).get("epoch") != checked.get("reply_epoch"):
            raise Conflict("Лид ответил или изменился подтверждённый канал. Проверьте адресата и текст заново.")
    if draft.kind == "broadcast":
        current = preview(db, actor, draft)
        if current["audience_versions"] != checked.get("audience_versions") or current["recipients"] != checked.get("recipients"):
            raise Conflict("Аудитория рассылки изменилась. Откройте предпросмотр заново.")
    # Re-run all validation against live roles, routes and versions immediately before committing.
    result = execute(db, actor, draft, publish=publish)
    draft.status = "applied"
    draft.version += 1
    draft.updated_at = now()
    if draft.kind == "substitution":
        draft.data = {**draft.data, "applied_plan": result.get("plan", [])}
    db.add(Audit(actor_id=actor.person_id, object_id=draft.id, action="UI_DRAFT_APPLIED",
                 old_version=expected, new_version=draft.version, data={"kind": draft.kind, "result_id": result.get("id")}))
    return result
