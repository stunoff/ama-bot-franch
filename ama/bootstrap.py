"""Server-only first administrator setup; reuses identities without moving accounts."""

from sqlalchemy import select
from ama import admin
from ama.config import settings
from ama.errors import BusinessError
from ama.models import Account, Binding, Role, now


def initialize(db, platform, user_id, chat_id, topic="", *, recover=False, transport):
    bot_id = settings().bot_id
    account = db.scalar(
        select(Account).where(Account.platform == platform, Account.external_id == str(user_id)).with_for_update()
    )
    if account and not account.active:
        raise BusinessError("Аккаунт отключён. Сначала выполните явное восстановление аккаунта.")
    admins = [
        r
        for r in db.scalars(
            select(Role).where(Role.bot_id == bot_id, Role.name == "admin", Role.active.is_(True)).with_for_update()
        )
        if not any((r.partner_id, r.brand_id, r.establishment_id))
        and (not r.expires_at or r.expires_at > now())
        and db.scalar(select(Account.id).where(Account.person_id == r.person_id, Account.active.is_(True)))
    ]
    if admins and (not recover or not account or not any(r.person_id == account.person_id for r in admins)):
        raise BusinessError(
            "Администратор уже установлен; используйте /af_settings. Для восстановления маршрута того же администратора добавьте --recover."
        )
    route = db.scalar(
        select(Binding)
        .where(
            Binding.bot_id == bot_id,
            Binding.platform == platform,
            Binding.chat_id == str(chat_id),
            Binding.topic_key == str(topic or ""),
        )
        .with_for_update()
    )
    if route and (route.purpose != "management" or any((route.partner_id, route.brand_id, route.establishment_id))):
        raise BusinessError("Чат уже имеет другое назначение/область. Выберите отдельный управляющий чат.")
    if account:
        person_id = account.person_id
    else:
        person_id = admin.apply(
            db, None, {"type": "person", "data": {"name": "Системный администратор"}}, bootstrap=True
        )["id"]
        admin.apply(
            db,
            None,
            {"type": "account", "data": {"person_id": person_id, "platform": platform, "external_id": user_id}},
            bootstrap=True,
        )
    if not admins:
        admin.apply(db, None, {"type": "role", "data": {"person_id": person_id, "name": "admin"}}, bootstrap=True)
    existing_route = route is not None
    if not route:
        result = admin.apply(
            db,
            None,
            {
                "type": "binding",
                "data": {"platform": platform, "chat_id": chat_id, "topic_key": topic, "purpose": "management"},
            },
            bootstrap=True,
        )
        route = db.get(Binding, result["id"])
    transport.send(
        route.platform,
        route.chat_id,
        route.topic_key,
        {"text": "АМА Франчайзинг: управляющий чат проверен. Настройки: /af_settings"},
    )
    # Identity and route changes roll back together when delivery fails.
    if existing_route:
        route.version += 1
    route.active, route.verified = True, True
    from ama.models import Audit

    db.add(
        Audit(
            actor_id=person_id,
            object_id=route.id,
            action="BOOTSTRAP_ADMIN_RECOVERED" if recover else "BOOTSTRAP_ADMIN_INITIALIZED",
            data={"platform": platform},
        )
    )
    return {"admin_person_id": person_id, "management_binding_id": route.id}
