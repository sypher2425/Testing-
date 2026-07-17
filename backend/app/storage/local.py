"""Local filesystem implementation of StorageBackend."""
from pathlib import Path
from typing import BinaryIO, Iterator

from app.storage.base import StorageBackend


class PathTraversalError(ValueError):
    pass


class LocalStorageBackend(StorageBackend):
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, relative_path: str) -> Path:
        """Resolve relative_path under root, refusing any escape via '..' or absolute paths."""
        candidate = (self.root / relative_path).resolve()
        root_resolved = self.root.resolve()
        if candidate != root_resolved and root_resolved not in candidate.parents:
            raise PathTraversalError(f"Path escapes storage root: {relative_path}")
        return candidate

    def save_stream(self, relative_path: str, stream: Iterator[bytes]) -> int:
        path = self._resolve(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        with open(path, "wb") as f:
            for chunk in stream:
                f.write(chunk)
                total += len(chunk)
        return total

    def save_bytes(self, relative_path: str, data: bytes) -> int:
        path = self._resolve(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return len(data)

    def get(self, relative_path: str) -> Path:
        return self._resolve(relative_path)

    def open_binary(self, relative_path: str) -> BinaryIO:
        return open(self._resolve(relative_path), "rb")

    def exists(self, relative_path: str) -> bool:
        try:
            return self._resolve(relative_path).exists()
        except PathTraversalError:
            return False

    def delete(self, relative_path: str) -> None:
        path = self._resolve(relative_path)
        if path.is_dir():
            import shutil

            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()

    def url_for(self, relative_path: str) -> str:
        return f"/api/jobs/{relative_path}"

    def list_dir(self, relative_path: str) -> list[str]:
        path = self._resolve(relative_path)
        if not path.exists():
            return []
        return sorted(p.name for p in path.iterdir())

    def size_of(self, relative_path: str) -> int:
        path = self._resolve(relative_path)
        if path.is_dir():
            return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        return path.stat().st_size if path.exists() else 0

    def ensure_dir(self, relative_path: str) -> None:
        self._resolve(relative_path).mkdir(parents=True, exist_ok=True)
