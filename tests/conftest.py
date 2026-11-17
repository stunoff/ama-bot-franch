import os
from datetime import timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from ama.db import Base
from ama.models import Identity, Role, Binding, Entity, Account, Record, Work, now
from ama.auth import Actor


@pytest.fixture(scope="session")
def engine():
    url = os.getenv("TEST_DATABASE_URL", "sqlite://")
    if not url.startswith("sqlite") and not url.split("?")[0].endswith("/ama_test"):
        raise RuntimeError("Integration tests only run against dedicated ama_test database")
    kw = {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool} if url == "sqlite://" else {}
    eng = create_engine(url, **kw)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def factory(engine):
    # DML очистка работает и на MySQL; данные тестов изолированы от боевой БД.
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def ctx(factory):
    db = factory()
    for person in ("fp", "observer", "curator", "marketing", "specialist", "admin", "consultant", "contract", "other"):
        db.add(Identity(id=person, name=person))
    db.flush()
    db.add_all([Entity(id="partner", kind="partner", name="Партнёр", data={}),
                Entity(id="brand", kind="brand", name="АМА Бургер", data={}),
                Entity(id="partner2", kind="partner", name="Другой", data={})])
    db.flush()
    db.add(Entity(id="est", kind="establishment", partner_id="partner", name="Кафе", data={}))
    db.flush()
    mapping = {"fp": "partner", "observer": "observer", "curator": "curator", "marketing": "marketing_manager",
               "specialist": "specialist", "admin": "admin", "consultant": "consultant", "contract": "contract", "other": "curator"}
    for person, role in mapping.items():
        db.add(Role(person_id=person, name=role, partner_id="partner" if person in {"fp", "observer"} else None))
        db.add(Account(person_id=person, platform="telegram", external_id=person))
    for key, purpose in [("client", "client"), ("support", "support"), ("management", "management"),
                         ("marketing", "marketing"), ("sales", "sales"), ("contracts", "contracts"), ("design", "design"), ("new", "support")]:
        db.add(Binding(id=key, platform="telegram", chat_id=key, purpose=purpose, active=True, verified=True,
                       partner_id="partner" if key == "client" else None, brand_id="brand" if key == "client" else None,
                       active_client_key="test-client" if key == "client" else None))
    calendar = {"timezone": "Europe/Moscow", "weekdays": [0, 1, 2, 3, 4], "start": "11:00", "end": "20:00", "holidays": []}
    db.add_all([Record(kind="calendar", key="support", data=calendar, published=True),
                Record(kind="route", key="support", published=True, data={"binding_id": "support", "owner_id": "curator", "reviewer_id": "curator", "calendar": "support"}),
                Record(kind="course", key="general", published=True, data={"cards": [
                    {"title": "Первый", "text": "Один", "required_materials": []},
                    {"title": "Второй", "text": "Два", "required_materials": []}]})])
    db.flush()
    w = Work(id="work", title="Проверка", kind="support", partner_id="partner", brand_id="brand", establishment_id="est",
             owner_id="fp", executor_id="fp", reviewer_id="curator", partner_person_id="fp", binding_id="support",
             client_binding_id="client", due_at=now() + timedelta(days=3), original_due_at=now() + timedelta(days=3), data={"calendar": "support"})
    db.add(w)
    db.commit()
    c = SimpleNamespace(db=db, factory=factory, w=w,
                        fp=Actor("fp", "client"), observer=Actor("observer", "client"),
                        curator=Actor("curator", "support"), admin=Actor("admin", "management"),
                        specialist=Actor("specialist", "support"), consultant=Actor("consultant", "sales"),
                        contract=Actor("contract", "contracts"), marketing=Actor("marketing", "support"))
    yield c
    db.rollback()
    db.close()
