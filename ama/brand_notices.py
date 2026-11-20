"""One scoped service notice per brand-link edition; never assigns a second general course."""

from hashlib import sha256
from sqlalchemy import select
from ama.models import BrandAccessNotice, EstablishmentBrand, Entity, Binding, Role, Record, Account
from ama.config import settings
from ama.auth import active_person_role
from ama.service import queue
from types import SimpleNamespace


def enqueue(db, link):
    if not link or not link.active:
        return
    db.flush()
    ident = sha256((settings().bot_id + link.id + str(link.version)).encode()).hexdigest()[:32]
    if not db.get(BrandAccessNotice, ident):
        db.add(
            BrandAccessNotice(id=ident, bot_id=settings().bot_id, link_id=link.id, link_version=link.version, data={})
        )


def context(db, notice):
    link = db.get(EstablishmentBrand, notice.link_id)
    if not link or not link.active or link.version != notice.link_version:
        return None
    est = db.get(Entity, link.establishment_id)
    brand = db.get(Entity, link.brand_id)
    if not est or not brand:
        return None
    area = SimpleNamespace(partner_id=est.partner_id, brand_id=brand.id, establishment_id=est.id)
    bindings = list(
        db.scalars(
            select(Binding).where(
                Binding.bot_id == notice.bot_id,
                Binding.partner_id == est.partner_id,
                Binding.brand_id == brand.id,
                Binding.purpose == "client",
                Binding.active.is_(True),
                Binding.verified.is_(True),
            )
        )
    )
    if len(bindings) != 1:
        return None
    b = bindings[0]
    people = {
        r.person_id
        for r in db.scalars(
            select(Role).where(Role.bot_id == notice.bot_id, Role.name == "partner", Role.active.is_(True))
        )
        if active_person_role(db, r.person_id, {"partner"}, area)
        and db.scalar(
            select(Account.id).where(
                Account.person_id == r.person_id, Account.platform == b.platform, Account.active.is_(True)
            )
        )
    }
    if not people:
        return None
    return b, area, brand, est, people


def tick(db):
    notice = db.scalar(
        select(BrandAccessNotice)
        .where(BrandAccessNotice.bot_id == settings().bot_id, BrandAccessNotice.status == "pending")
        .order_by(BrandAccessNotice.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if not notice:
        return False
    link = db.get(EstablishmentBrand, notice.link_id)
    if not link or not link.active or link.version != notice.link_version:
        notice.status = "cancelled"
        return True
    ctx = context(db, notice)
    if not ctx:
        # Keep it durable, but move behind other notices until the owner connects a route.
        notice.status = "waiting_route"
        notice.data = {"reason": "Нужен проверенный брендовый чат и действующий ФП"}
        return True
    b, area, brand, est, people = ctx
    from ama.learning import can_material
    from ama.auth import Actor

    available = any(
        can_material(db, Actor(person, b.id), r, establishment_id=est.id)
        for person in people
        for r in db.scalars(select(Record).where(Record.kind == "material", Record.published.is_(True)))
        if r.data.get("brand_id") == brand.id
    )
    text = (
        "Вам открыт доступ к материалам " + brand.name + ".\nБрендовая база знаний доступна в меню «База знаний»."
        if available
        else "Подключён бренд "
        + brand.name
        + ". Материалы появятся в «Базе знаний» после публикации и проверки владельцем."
    )
    row = queue(
        db,
        b,
        "brand-access:" + notice.id,
        text + "\nПредприятие: " + est.name + "\nОбщий курс не начинается заново.",
        buttons=[["База знаний", "af:knowledge"]],
    )
    row.payload = {**row.payload, "brand_access_notice": notice.id, "partner_window": True}
    db.flush()
    notice.status = "queued"
    notice.data = {"binding_id": b.id, "people": sorted(people), "outbox_id": row.id}
    return True


def wake(db):
    for row in db.scalars(
        select(BrandAccessNotice).where(
            BrandAccessNotice.bot_id == settings().bot_id, BrandAccessNotice.status == "waiting_route"
        )
    ):
        if context(db, row):
            row.status = "pending"


def deliverable(db, row):
    ident = row.payload.get("brand_access_notice")
    if not ident:
        return True
    notice = db.get(BrandAccessNotice, ident)
    ctx = context(db, notice) if notice else None
    return bool(ctx and ctx[0].id == row.binding_id and ctx[4].intersection(notice.data.get("people", [])))
