from sqlalchemy import select
from ama.auth import require, roles_for
from ama.errors import BusinessError, AccessDenied
from ama.models import Broadcast, Binding, now
from ama.service import queue, stamp, versioned


def preview(db, actor, text, brand_id=None):
    require(db, actor, {"marketing_manager", "support_head", "curator"})
    if db.get(Binding, actor.binding_id).purpose == "client":
        raise AccessDenied("Рассылки создаются из внутреннего рабочего чата.")
    from ama.packets import publisher
    publisher(db,actor,general=brand_id is None)
    bindings = []
    for b in db.scalars(select(Binding).where(Binding.purpose == "client", Binding.active.is_(True), Binding.verified.is_(True))):
        if brand_id and b.brand_id != brand_id:
            continue
        # Не расширять область роли массовым действием.
        from types import SimpleNamespace
        scope = SimpleNamespace(binding_id=actor.binding_id, client_binding_id=b.id, pending_binding_id=None,
                                partner_id=b.partner_id, brand_id=b.brand_id, establishment_id=b.establishment_id)
        if roles_for(db, actor, scope) & {"marketing_manager", "support_head", "curator"}:
            publisher(db,actor,scope,general=brand_id is None)
            partner = db.get(__import__('ama.models', fromlist=['Entity']).Entity, b.partner_id)
            if not partner or not partner.data.get("notification_window"):
                raise BusinessError("У адресата не настроено согласованное окно уведомлений.")
            bindings.append(b)
    if not bindings or not text.strip():
        raise BusinessError("Пустая аудитория или текст.")
    b = Broadcast(author_id=actor.person_id, text=text, binding_ids=[r.id for r in bindings],
                  audience_versions={r.id: r.version for r in bindings})
    db.add(b)
    db.flush()
    stamp(db, actor, b, "BROADCAST_PREVIEW")
    return b


def publish(db, actor, broadcast_id, expected):
    require(db, actor, {"marketing_manager", "support_head", "curator"})
    b = db.scalar(select(Broadcast).where(Broadcast.id == broadcast_id).with_for_update())
    if not b or b.author_id != actor.person_id:
        raise AccessDenied("Предпросмотр рассылки недоступен.")
    from ama.packets import publisher
    publisher(db,actor)
    if b.published_at:
        return b
    versioned(b, expected)
    for binding_id in b.binding_ids:
        route = db.get(Binding, binding_id)
        from types import SimpleNamespace
        scope = SimpleNamespace(binding_id=actor.binding_id, client_binding_id=binding_id,
                                partner_id=route.partner_id if route else None, brand_id=route.brand_id if route else None,
                                establishment_id=route.establishment_id if route else None)
        publisher(db,actor,scope)
        if not roles_for(db, actor, scope) & {"marketing_manager", "support_head", "curator"}:
            raise AccessDenied("Область роли изменилась после предпросмотра аудитории.")
        if not route or route.version != b.audience_versions[binding_id] or not route.active:
            raise BusinessError("Аудитория изменилась. Подготовьте новый предпросмотр.")
        msg = queue(db, route, f"broadcast:{b.id}:{binding_id}", b.text)
        msg.payload = {**msg.payload, "partner_window": True}
    b.published_at, b.version = now(), b.version + 1
    stamp(db, actor, b, "BROADCAST_PUBLISHED")
    return b
