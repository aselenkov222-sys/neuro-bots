from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.engine import Engine

DATABASE_URL = "postgresql+psycopg://neurobots@127.0.0.1:5432/neurobots"

metadata = MetaData()


def create_db_engine() -> Engine:
    return create_engine(DATABASE_URL)


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
