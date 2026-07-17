from collections.abc import Generator

from sqlalchemy.orm import Session

from app.database import get_session


def db_session() -> Generator[Session, None, None]:
    session = get_session()
    try:
        yield session
    finally:
        session.close()
