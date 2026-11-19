from types import SimpleNamespace
from sqlalchemy import select
from ama.models import Identity, Account, Role, Entity, EstablishmentBrand, Binding, Record, Work, Lead, Deal, now
from ama.config import settings
from ama.errors import AccessDenied
from ama import admin
from ama.ui import access, schema

ADMIN_CATALOGS = {
    "bindings": ("Чаты", Binding), "people": ("Люди", Identity), "accounts": ("Аккаунты", Account), "roles": ("Роли и замещения", Role),
    "partner": ("Франчайзи-партнёры", Entity), "establishment": ("Предприятия", Entity), "brand": ("Бренды", Entity),
    "links": ("Бренды предприятий", EstablishmentBrand),
    **{f"record:{k}": (label, Record) for k, label in {
        "calendar": "Рабочие календари", "material": "Материалы и ссылки", "course": "Ознакомление ФП", "route": "Маршруты работ",
        "commercial": "Коммерческие условия", "consent": "Согласия", "template": "Тексты сообщений", "policy": "Сценарии и правила"}.items()},
}
POLICY_LABELS = {"broadcast_permission":"Права рассылки ФП","sales_content": "Карточки и материалы продаж", "scenario": "Сценарии и чек-листы", "source": "Источники продаж", "faq": "База ответов консультанта",
                 "sla": "Сроки и эскалации", "payment": "Платные работы и реквизиты", "schedule": "Расписания и отчёты", "generic": "Прочие правила"}


def name(db, person_id):
    p = db.get(Identity, person_id) if person_id else None
    return p.name if p else "не назначен"


def entity_name(db, entity_id):
    e = db.get(Entity, entity_id) if entity_id else None
    return e.name if e else "не выбрано"


def label(db, obj):
    if isinstance(obj, Identity):
        return obj.name
    if isinstance(obj, Entity):
        return obj.name + (" · архив" if obj.data.get("archived") else "")
    if isinstance(obj, Account):
        return f"{name(db, obj.person_id)} · {obj.platform} · {obj.external_id}" + (" · отключён" if not obj.active else "")
    if isinstance(obj, Role):
        return f"{name(db, obj.person_id)} · {schema.ROLES.get(obj.name, obj.name)}" + (" · отключена" if not obj.active else "")
    if isinstance(obj, Binding):
        return f"{schema.PURPOSES.get(obj.purpose, obj.purpose)} · {obj.platform} · {obj.chat_id}" + (" · активен" if obj.active and obj.verified else " · требует настройки")
    if isinstance(obj, Record):
        return str(obj.data.get("title") or obj.key) + (" · опубликовано" if obj.published else " · черновик")
    if isinstance(obj, EstablishmentBrand):
        return entity_name(db, obj.establishment_id) + " · " + entity_name(db, obj.brand_id) + (" · отключена" if not obj.active else "")
    if isinstance(obj, Work):
        return obj.title
    if isinstance(obj, Lead):
        return str(obj.data.get("name") or name(db, obj.person_id)) + " · " + schema.STAGES.get(obj.stage, obj.stage)
    if isinstance(obj, Deal):
        lead = db.get(Lead, obj.lead_id)
        return (label(db, lead) if lead else "Сделка") + (" · успех" if obj.successful_at else " · оформление")
    return str(getattr(obj, "title", getattr(obj, "id", "Запись")))


def is_admin(db, actor):
    try:
        admin.check_admin(db, actor)
        return True
    except AccessDenied:
        return False


def catalog(db, actor, key, search=""):
    admin.check_admin(db, actor)
    base = key if key in ADMIN_CATALOGS else "record:policy" if key.startswith("policy:") else None
    if not base:
        return []
    model = ADMIN_CATALOGS[base][1]
    query = select(model)
    if model in {Role, Binding}:
        query = query.where(model.bot_id == settings().bot_id)
    if base in {"partner", "establishment", "brand"}:
        query = query.where(Entity.kind == base)
    if base.startswith("record:"):
        query = query.where(Record.kind == base.split(":")[1])
    rows = list(db.scalars(query.order_by(model.id)))
    if key.startswith("policy:"):
        rows = [r for r in rows if r.data.get("policy_type", "generic") == key.split(":")[1]]
    needle = search.casefold().strip()
    return [r for r in rows if not needle or needle in label(db, r).casefold() or needle in r.id.casefold()
            or isinstance(r, Record) and needle in r.key.casefold()]


def scope_for_draft(db, draft):
    d = draft.data
    if draft.target_id and (draft.kind.startswith(("work:", "operation:"))):
        w = db.get(Work, draft.target_id)
        if w:
            return w
    if draft.target_id and draft.kind.startswith("case:"):
        from ama.sale_flow import get
        target = get(db, draft.target_id)
        return SimpleNamespace(partner_id=None, brand_id=target.brand_id, establishment_id=None)
    if draft.target_id and draft.kind.startswith(("sales:", "contract:", "deal:")):
        from ama.models import Lead, Deal
        target = db.get(Deal if draft.kind.startswith("deal:") else Lead, draft.target_id)
        if target:
            return SimpleNamespace(partner_id=d.get("partner_id") or target.data.get("partner_id"),
                                   brand_id=d.get("brand_id") or target.data.get("brand_id"), establishment_id=None)
    values = d.get("value", d) if draft.kind.startswith("record:") else d
    return SimpleNamespace(partner_id=values.get("partner_id"), brand_id=values.get("brand_id"), establishment_id=values.get("establishment_id"))


def refs(db, actor, source, draft=None, search=""):
    technical = is_admin(db, actor)
    context = scope_for_draft(db, draft) if draft else SimpleNamespace(partner_id=None, brand_id=None, establishment_id=None)
    rows = []
    if source == "packet_asset":
        from ama.models import PacketAsset
        rows=[(a.id,a.filename+" · "+a.status) for a in db.scalars(select(PacketAsset).where(PacketAsset.author_id==actor.person_id).order_by(PacketAsset.id))]
    elif source in {"packet_checklist","packet_establishment"}:
        from ama.models import PacketObligation,PacketRevision
        from ama import packets
        if source=="packet_checklist":
            ob=db.get(PacketObligation,draft.target_id)
            if ob:
                packets.authorize_ob(db,actor,ob,True)
                r=db.get(PacketRevision,ob.revision_id)
                rows=[(item,item) for item in r.data.get("checklist",[])]
        else:
            r=db.get(PacketRevision,draft.target_id)
            if r:
                rows=list({(i["establishment_id"],i["name"]) for i in packets.visible_items(db,actor,r)})
    elif source in {"partner", "brand", "establishment"}:
        for e in db.scalars(select(Entity).where(Entity.kind == source).order_by(Entity.name, Entity.id)):
            if e.data.get("archived") or not technical and not access.can_entity(db, actor, e):
                continue
            if source == "establishment" and context.partner_id and e.partner_id != context.partner_id:
                continue
            if source == "establishment" and context.brand_id and not db.scalar(select(EstablishmentBrand).where(EstablishmentBrand.establishment_id == e.id, EstablishmentBrand.brand_id == context.brand_id, EstablishmentBrand.active.is_(True))):
                continue
            rows.append((e.id, label(db, e)))
    elif source in {"person", "partner_person"}:
        for p in db.scalars(select(Identity).order_by(Identity.name, Identity.id)):
            roles = list(db.scalars(select(Role).where(Role.bot_id == settings().bot_id, Role.person_id == p.id, Role.active.is_(True))))
            valid = [r for r in roles if (not r.expires_at or r.expires_at > now()) and access.matches(r, context)]
            if source == "partner_person" and not any(r.name == "partner" for r in valid):
                continue
            if not technical and not any(r.name in access.STAFF | {"partner"} for r in valid):
                continue
            rows.append((p.id, p.name))
    elif source in {"binding", "client_binding"}:
        for b in db.scalars(select(Binding).where(Binding.bot_id == settings().bot_id).order_by(Binding.purpose, Binding.id)):
            if source == "client_binding" and b.purpose != "client" or source == "binding" and b.purpose == "client":
                continue
            if not technical and (not b.active or not b.verified):
                continue
            if any(getattr(b, k) and getattr(b, k) != getattr(context, k) for k in ("partner_id", "brand_id", "establishment_id")):
                continue
            rows.append((b.id, label(db, b)))
    elif source.startswith("record:"):
        kind = source.split(":")[1]
        for r in db.scalars(select(Record).where(Record.kind == kind).order_by(Record.key)):
            if not technical and not r.published:
                continue
            brand = r.data.get("brand_id")
            if brand and context.brand_id and brand != context.brand_id:
                continue
            if kind == "material" and not technical:
                # Employees only see records intended for an effective role in this scope.
                if not any(role.name in r.data.get("audience", []) and access.matches(role, context) for role in access.assignments(db, actor)):
                    continue
            rows.append((r.key, label(db, r)))
    elif source == "sales_expert" and draft and draft.kind.startswith("case:"):
        from ama.models import SalesExpert
        rows = [(e.id, e.question[:90]) for e in db.scalars(select(SalesExpert).where(SalesExpert.case_id==draft.target_id, SalesExpert.state=="answered")) if e.data.get("approved_exception")]
    elif source == "sales_case" and draft and draft.kind.startswith("case:"):
        from ama.models import SalesCase
        from ama.sale_flow import get, can_case
        current = get(db, draft.target_id)
        rows = [(c.id, c.data.get("name", "Интерес")+" · "+entity_name(db,c.brand_id)) for c in db.scalars(select(SalesCase).where(SalesCase.lead_id==current.lead_id,SalesCase.id!=current.id))
                if c.result not in {"success","refused","not_target"} and can_case(db,actor,c) and c.owner_id==actor.person_id]
    elif source == "work":
        query = select(Work).order_by(Work.due_at, Work.id)
        if draft and draft.kind == "substitution":
            query = query.where(Work.status != "done")
        for w in db.scalars(query):
            if not technical and not access.can_work(db, actor, w):
                continue
            if context.partner_id and w.partner_id != context.partner_id or context.establishment_id and w.establishment_id not in {context.establishment_id, None}:
                continue
            rows.append((w.id, w.title + " · " + entity_name(db, w.establishment_id)))
    needle = search.casefold().strip()
    return [(key, title) for key, title in rows if not needle or needle in title.casefold() or needle in key.casefold()]


def reference_label(db, source, value):
    if not value:
        return "не выбрано"
    if source=="packet_checklist":
        return str(value)
    if source=="packet_asset":
        from ama.models import PacketAsset
        asset=db.get(PacketAsset,value)
        return asset.filename if asset else "Файл не найден"
    if source=="packet_establishment":
        return entity_name(db,value)
    if source.startswith("record:"):
        r = db.scalar(select(Record).where(Record.kind == source.split(":")[1], Record.key == value))
    elif source == "sales_expert":
        from ama.models import SalesExpert
        r = db.get(SalesExpert, value)
    elif source == "sales_case":
        from ama.models import SalesCase
        r = db.get(SalesCase, value)
    else:
        model = {"person": Identity, "partner_person": Identity, "binding": Binding, "client_binding": Binding,
                 "work": Work, "partner": Entity, "brand": Entity, "establishment": Entity}.get(source)
        r = db.get(model, value) if model else None
    return label(db, r) if r else "Запись отсутствует"
