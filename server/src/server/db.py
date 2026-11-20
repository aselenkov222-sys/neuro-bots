import os

from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.engine import Engine

DATABASE_URL = "postgresql+psycopg://neurobots@127.0.0.1:5432/neurobots"

metadata = MetaData()


def database_url() -> str:
    configured = os.environ.get("NEURO_BOTS_DATABASE_URL")
    if configured is None or configured == "":
        return DATABASE_URL
    return configured


def alembic_sqlalchemy_url() -> str:
    return (database_url() or DATABASE_URL).replace("%", "%%")


def create_db_engine() -> Engine:
    return create_engine(database_url())


def database_responds(engine: Engine | None = None) -> bool:
    if engine is None:
        engine = create_db_engine()
        dispose = True
    else:
        dispose = False
    try:
        with engine.connect() as connection:
            value = connection.execute(text("SELECT 1")).scalar_one()
        return value == 1
    finally:
        if dispose:
            engine.dispose()
