from app.storage.base import StorageBackend
from app.storage.local import LocalStorageBackend

__all__ = ["StorageBackend", "LocalStorageBackend", "get_storage"]

_instance: StorageBackend | None = None


def get_storage() -> StorageBackend:
    global _instance
    if _instance is None:
        from app.config import get_settings

        _instance = LocalStorageBackend(get_settings().jobs_path)
    return _instance
