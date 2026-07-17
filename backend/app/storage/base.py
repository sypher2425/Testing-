"""Abstract storage interface so the local filesystem can be swapped for S3/R2 later."""
from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO, Iterator


class StorageBackend(ABC):
    """All paths are relative to a job's namespace, e.g. '{job_id}/source/video.mp4'."""

    @abstractmethod
    def save_stream(self, relative_path: str, stream: Iterator[bytes]) -> int:
        """Write a stream of bytes to relative_path. Returns total bytes written."""

    @abstractmethod
    def save_bytes(self, relative_path: str, data: bytes) -> int:
        """Write raw bytes to relative_path. Returns total bytes written."""

    @abstractmethod
    def get(self, relative_path: str) -> Path:
        """Return an absolute filesystem path for reading relative_path."""

    @abstractmethod
    def open_binary(self, relative_path: str) -> BinaryIO:
        """Open relative_path for binary reading."""

    @abstractmethod
    def exists(self, relative_path: str) -> bool:
        ...

    @abstractmethod
    def delete(self, relative_path: str) -> None:
        """Delete a file or, if relative_path is a directory, delete it recursively."""

    @abstractmethod
    def url_for(self, relative_path: str) -> str:
        """Return an API-relative URL that can be used to fetch this file."""

    @abstractmethod
    def list_dir(self, relative_path: str) -> list[str]:
        ...

    @abstractmethod
    def size_of(self, relative_path: str) -> int:
        ...

    @abstractmethod
    def ensure_dir(self, relative_path: str) -> None:
        ...
