from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine():
    settings = get_settings()
    url = settings.DATABASE_URL
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _ensure_schema_columns() -> None:
    """Forward-only micro-migration: add any column that exists on the models
    but not in an existing database, via ALTER TABLE ADD COLUMN. create_all()
    only creates missing tables, so without this an old data volume breaks the
    moment a release adds a column (requiring a destructive `down -v`). Not a
    full migration system — it never renames, drops, or retypes anything."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column.type.compile(engine.dialect)}'
            default_arg = getattr(column.default, "arg", None)
            if not column.nullable and isinstance(default_arg, (str, int, float, bool)) and not callable(default_arg):
                if isinstance(default_arg, bool):
                    literal = str(int(default_arg))
                elif isinstance(default_arg, str):
                    literal = "'" + default_arg.replace("'", "''") + "'"
                else:
                    literal = str(default_arg)
                ddl += f" NOT NULL DEFAULT {literal}"
            with engine.begin() as conn:
                conn.execute(text(ddl))


def init_db() -> None:
    from app import models  # noqa: F401 register mappers

    settings = get_settings()
    if settings.DATABASE_URL.startswith("sqlite"):
        # Removing the three-slash SQLite prefix already leaves the correct
        # absolute path on both platforms: `/data/...` on Linux and `C:\...`
        # on Windows. Prepending another slash produces the invalid `\C:\...`.
        db_path = settings.DATABASE_URL.replace("sqlite:///", "", 1)
        from pathlib import Path

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    _ensure_schema_columns()


def get_session() -> Session:
    return SessionLocal()
