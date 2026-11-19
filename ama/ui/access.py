"""Read scopes for dashboards; every mutation still calls its domain service."""
import hashlib
import json
from types import SimpleNamespace
from sqlalchemy import select
from ama.auth import roles_for
from ama.config import settings
from ama.errors import AccessDenied
from ama.models import Role, Account, Binding, Entity, EstablishmentBrand, Work, Lead, now

STAFF = {"lawyer", "leadership", "curator", "support_head", "marketing_manager", "specialist", "contract", "consultant", "chief_marketer"}
AREAS = {"lawyer":{"legal"},"leadership":{"leadership"},"curator": {"support"}, "support_head": {"support", "management"}, "marketing_manager": {"marketing"},
         "consultant": {"sales"}, "contract": {"contracts"}, "chief_marketer": {"chief_marketer"}}


def assignments(db, actor):
    b = db.get(Binding, actor.binding_id)
    if not b or not b.active or not b.verified or b.bot_id != settings().bot_id:
        raise AccessDenied("Привязка чата отключена. Откройте меню в действующем чате.")
    return [r for r in db.scalars(select(Role).where(Role.person_id == actor.person_id, Role.bot_id == settings().bot_id,
                                                     Role.active.is_(True)).order_by(Role.id))
            if (not r.expires_at or r.expires_at > now()) and all(
                not getattr(b, key) or not getattr(r, key) or getattr(b, key) == getattr(r, key)
                for key in ("partner_id", "brand_id", "establishment_id"))]


def matches(role, obj):
    return all(not getattr(role, key) or getattr(role, key) == getattr(obj, key, None)
               for key in ("partner_id", "brand_id", "establishment_id"))


def staff_role(db, actor, role):
    purpose = db.get(Binding, actor.binding_id).purpose
    if purpose == "client":
        return False
    if role == "specialist":
        return purpose not in {"client", "management", "sales"}
    return purpose in AREAS.get(role, set())


def staff(db, actor):
    return any(r.name in STAFF and staff_role(db, actor, r.name) for r in assignments(db, actor))


def require_staff(db, actor):
    if not staff(db, actor):
        raise AccessDenied("Рабочее место доступно назначенному сотруднику в профильном чате.")


def scope(db, actor, obj, names=STAFF):
    return any(r.name in names and matches(r, obj) and staff_role(db, actor, r.name) for r in assignments(db, actor))


def domain_scope(actor, obj):
    return SimpleNamespace(binding_id=actor.binding_id, client_binding_id=None,
                           partner_id=getattr(obj, "partner_id", None), brand_id=getattr(obj, "brand_id", None),
                           establishment_id=getattr(obj, "establishment_id", None))


def can_work(db, actor, w, *, detail=False):
    try:
        assigned = assignments(db, actor)
        if not assigned:
            return False
        purpose = db.get(Binding, actor.binding_id).purpose
        direct = bool({r for r in roles_for(db, actor, w) if r in STAFF and staff_role(db, actor, r)
                       or r in {"partner", "observer"} and purpose == "client"})
        if direct:
            return True
    except AccessDenied:
        if detail:
            return False
    if detail:
        return False
    return scope(db, actor, w, {"curator", "support_head", "marketing_manager"}) and (
        not scope(db, actor, w, {"marketing_manager"}) or scope(db, actor, w, {"curator", "support_head"})
        or w.kind.startswith("marketing") or w.kind in {"design", "smm", "advertising", "printing", "menu"})


def works(db, actor):
    require_staff(db, actor)
    return [w for w in db.scalars(select(Work).order_by(Work.due_at, Work.id)) if can_work(db, actor, w)]


def can_entity(db, actor, entity):
    if entity.kind == "establishment":
        brand_ids = list(db.scalars(select(EstablishmentBrand.brand_id).where(EstablishmentBrand.establishment_id == entity.id,
                                                                             EstablishmentBrand.active.is_(True)))) or [None]
        return any(scope(db, actor, SimpleNamespace(partner_id=entity.partner_id, establishment_id=entity.id, brand_id=b)) for b in brand_ids)
    if entity.kind == "partner":
        if scope(db, actor, SimpleNamespace(partner_id=entity.id, brand_id=None, establishment_id=None)):
            return True
        return any(can_entity(db, actor, e) for e in db.scalars(select(Entity).where(Entity.kind == "establishment", Entity.partner_id == entity.id)))
    if entity.kind == "brand":
        return any(r.name in STAFF and staff_role(db, actor, r.name) and not r.partner_id and not r.establishment_id
                   and (not r.brand_id or r.brand_id == entity.id) for r in assignments(db, actor)) or any(
                       scope(db, actor, SimpleNamespace(partner_id=e.partner_id, establishment_id=e.id, brand_id=entity.id)) for e in db.scalars(select(Entity).join(EstablishmentBrand, EstablishmentBrand.establishment_id == Entity.id)
                                                                  .where(EstablishmentBrand.brand_id == entity.id, EstablishmentBrand.active.is_(True))))
    return False


def can_lead(db, actor, lead, *, for_contract=False):
    wanted = {"contract"} if for_contract else {"consultant"}
    obj = SimpleNamespace(partner_id=None, establishment_id=None, brand_id=lead.data.get("brand_id"))
    if not scope(db, actor, obj, wanted):
        return False
    return for_contract or lead.owner_id in {None, actor.person_id}


def leads(db, actor):
    return [lead for lead in db.scalars(select(Lead).order_by(Lead.created_at.desc())) if can_lead(db, actor, lead)]


def can_deal(db, actor, deal):
    lead = db.get(Lead, deal.lead_id)
    if not lead:
        return False
    if deal.case_id:
        from ama.models import SalesCase
        ids=deal.data.get("case_ids") or [deal.case_id]
        cases=list(db.scalars(select(SalesCase).where(SalesCase.id.in_(ids))))
        if len(cases)!=len(set(ids)):
            return False
        if all(scope(db,actor,SimpleNamespace(partner_id=None,brand_id=c.brand_id,establishment_id=None),{"contract"}) for c in cases):
            return True
        return deal.curator_id==actor.person_id and all(scope(db,actor,SimpleNamespace(partner_id=deal.data.get("partner_id"),brand_id=c.brand_id,establishment_id=None),{"curator","support_head"}) for c in cases)
    if can_lead(db, actor, lead, for_contract=True):
        return True
    obj = SimpleNamespace(partner_id=deal.data.get("partner_id"), brand_id=deal.data.get("brand_id"), establishment_id=None)
    return deal.curator_id == actor.person_id and scope(db, actor, obj, {"curator", "support_head"})


def grants_hash(db, actor):
    b = db.get(Binding, actor.binding_id)
    rows = assignments(db, actor)
    accounts = list(db.scalars(select(Account).where(Account.person_id == actor.person_id, Account.active.is_(True)).order_by(Account.id)))
    if not accounts:
        raise AccessDenied("Аккаунты сотрудника отключены.")
    payload = {"bot": settings().bot_id, "person": actor.person_id, "binding": b.id, "binding_version": b.version,
               "accounts": [{"id": a.id, "version": a.version} for a in accounts],
               "roles": [{"id": r.id, "version": r.version, "name": r.name, "partner": r.partner_id, "brand": r.brand_id,
                          "establishment": r.establishment_id, "expires": r.expires_at.isoformat() if r.expires_at else None} for r in rows]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
