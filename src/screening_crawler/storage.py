"""Storage abstractions; local files are one replaceable backend."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from .canonical import sha256_bytes
from .errors import IntegrityError


class BlobStore(Protocol):
    def put_verified(self, key: str, data: bytes, sha256: str) -> None: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...


class LocalBlobStore:
    """Content-addressed local storage using exclusive, atomic creation."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _resolve(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        root = self.root.resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError("storage key escapes configured root")
        return candidate

    def put_verified(self, key: str, data: bytes, sha256: str) -> None:
        if sha256_bytes(data) != sha256:
            raise IntegrityError("refusing to store bytes under an incorrect digest")
        destination = self._resolve(key)
        if destination.exists():
            if sha256_bytes(destination.read_bytes()) != sha256:
                raise IntegrityError(f"existing content-addressed object is corrupt: {key}")
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.staging")
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()

    def get(self, key: str) -> bytes:
        return self._resolve(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()


class MemoryBlobStore:
    """Synthetic-test backend implementing the same integrity contract."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put_verified(self, key: str, data: bytes, sha256: str) -> None:
        if sha256_bytes(data) != sha256:
            raise IntegrityError("digest mismatch")
        previous = self._objects.get(key)
        if previous is not None and previous != data:
            raise IntegrityError("content-addressed key collision")
        self._objects[key] = bytes(data)

    def get(self, key: str) -> bytes:
        return self._objects[key]

    def exists(self, key: str) -> bool:
        return key in self._objects

