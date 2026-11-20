"""Publication and activation checks shared by bootstrap/configuration paths."""

from types import SimpleNamespace
from sqlalchemy import select
from ama.config import settings
from ama.errors import BusinessError
from ama.models import Account, Binding, Role, now
from ama.auth import Actor, active_person_role


def partner_available(db, binding):
    for role in db.scalars(
        select(Role).where(
            Role.bot_id == settings().bot_id,
            Role.name == "partner",
            Role.active.is_(True),
            Role.partner_id == binding.partner_id,
        )
    ):
        if role.expires_at and role.expires_at <= now():
            continue
        if any(getattr(role, k) and getattr(role, k) != getattr(binding, k) for k in ("brand_id", "establishment_id")):
            continue
        if db.scalar(
            select(Account.id).where(
                Account.person_id == role.person_id, Account.platform == binding.platform, Account.active.is_(True)
            )
        ):
            return True
    return False


def validate_route(db, value, key=None):
    from ama.ui.access import STAFF, staff_role
    from ama.service import MARKETING_RESULTS, record

    b = db.get(Binding, value.get("binding_id"))
    if not b or b.bot_id != settings().bot_id or b.purpose == "client" or not b.active or not b.verified:
        raise BusinessError("Маршрут требует активный проверенный внутренний чат текущего бота.")
    if any(getattr(b, k) and getattr(b, k) != value.get(k) for k in ("partner_id", "brand_id", "establishment_id")):
        raise BusinessError("Область маршрута не совпадает с областью рабочего чата.")
    if not value.get("calendar"):
        raise BusinessError("Маршрут требует опубликованный календарь.")
    record(db, "calendar", value["calendar"])
    scope = SimpleNamespace(**{k: value.get(k) for k in ("partner_id", "brand_id", "establishment_id")})
    owner = value.get("owner_id")
    if not owner or not any(
        staff_role(db, Actor(owner, b.id), name) and active_person_role(db, owner, {name}, scope) for name in STAFF
    ):
        raise BusinessError("Исполнитель маршрута не имеет действующей профильной роли в рабочем чате.")
    expected = (
        {"curator", "support_head"}
        if key == "support"
        else {"marketing_manager"}
        if key and (key.startswith("marketing") or key in MARKETING_RESULTS)
        else STAFF
    )
    reviewer = value.get("reviewer_id")
    if not reviewer or not active_person_role(db, reviewer, expected, scope):
        raise BusinessError("Принимающий маршрута не имеет требуемой действующей роли.")
    if key in {"support", "marketing"} and not any(
        staff_role(db, Actor(reviewer, b.id), name) and active_person_role(db, reviewer, {name}, scope)
        for name in expected
    ):
        raise BusinessError("Принимающий не может выполнять действия в функциональном назначении чата.")
    for person_id in (owner, reviewer):
        if not db.scalar(
            select(Account.id).where(
                Account.person_id == person_id, Account.platform == b.platform, Account.active.is_(True)
            )
        ):
            raise BusinessError("У исполнителя/принимающего отсутствует активный аккаунт в мессенджере маршрута.")
    return b
