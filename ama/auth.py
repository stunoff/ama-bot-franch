from dataclasses import dataclass
from sqlalchemy import select
from ama.models import Account, Binding, Role, Work, now
from ama.config import settings
from ama.errors import AccessDenied


@dataclass(frozen=True)
class Actor:
    person_id: str
    binding_id: str


def identify(db, platform, user_id, chat_id, topic_key=""):
    account = db.scalar(select(Account).where(Account.platform == platform, Account.external_id == str(user_id), Account.active.is_(True)))
    binding = db.scalar(select(Binding).where(Binding.bot_id == settings().bot_id, Binding.platform == platform,
                                            Binding.chat_id == str(chat_id), Binding.topic_key == str(topic_key or ""),
                                            Binding.active.is_(True), Binding.verified.is_(True)))
    if not account or not binding:
        raise AccessDenied("Учётная запись или чат не подключены. Обратитесь к куратору.")
    return Actor(account.person_id, binding.id)


def roles_for(db, actor, obj=None):
    binding = db.get(Binding, actor.binding_id)
    if not binding or binding.bot_id != settings().bot_id or not binding.active or not binding.verified:
        raise AccessDenied("Привязка чата отключена.")
    if obj:
        allowed = {obj.binding_id, obj.client_binding_id}
        if isinstance(obj, Work) and obj.pending_binding_id:
            allowed.add(obj.pending_binding_id)
        if binding.id not in allowed:
            raise AccessDenied("Карточка недоступна из этого чата/темы.")
        for field in ("partner_id", "brand_id", "establishment_id"):
            value = getattr(binding, field)
            if value and value != getattr(obj, field):
                raise AccessDenied("Область чата не совпадает с карточкой.")
    result = []
    for role in db.scalars(select(Role).where(Role.person_id == actor.person_id, Role.bot_id == settings().bot_id,
                                             Role.active.is_(True))):
        if role.expires_at and role.expires_at <= now():
            continue
        scope = obj or binding
        if any(getattr(role, k) and (obj is not None or getattr(scope, k, None) is not None)
               and getattr(role, k) != getattr(scope, k, None)
               for k in ("partner_id", "brand_id", "establishment_id")):
            continue
        result.append(role.name)
    return set(result)


def require(db, actor, names, obj=None):
    granted = roles_for(db, actor, obj)
    match = granted & set(names)
    if not match:
        raise AccessDenied("Нет действующего назначения для этого действия и области.")
    return sorted(match)[0]


def active_person_role(db, person_id, names, obj):
    for r in db.scalars(select(Role).where(Role.person_id == person_id, Role.bot_id == settings().bot_id,
                                          Role.active.is_(True), Role.name.in_(names))):
        if r.expires_at and r.expires_at <= now():
            continue
        if all(not getattr(r, k) or getattr(r, k) == getattr(obj, k, None)
               for k in ("partner_id", "brand_id", "establishment_id")):
            return True
    return False
