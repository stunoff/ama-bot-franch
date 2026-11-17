from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from ama.config import settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    return create_engine(url, pool_pre_ping=True, **({"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}))


engine = make_engine(settings().database_url)
Session = sessionmaker(bind=engine, expire_on_commit=False)
