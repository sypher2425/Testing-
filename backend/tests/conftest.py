import os
import tempfile
from pathlib import Path

_tmp_root = tempfile.mkdtemp(prefix="video_dataset_test_")
os.environ["DATA_DIR"] = str(Path(_tmp_root) / "data")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_root}/test.db"
os.environ["REDIS_URL"] = "redis://localhost:6379/15"
os.environ["WHISPER_MODEL_SIZE"] = "tiny"
os.environ["MIN_FREE_DISK_MB"] = "1"

import pytest  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database import Base, engine, get_session, init_db  # noqa: E402
from app.storage import get_storage  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _init_database():
    init_db()
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture()
def db_session():
    session = get_session()
    yield session
    for table in reversed(Base.metadata.sorted_tables):
        session.execute(table.delete())
    session.commit()
    session.close()


@pytest.fixture()
def storage():
    return get_storage()


@pytest.fixture()
def settings():
    return get_settings()
