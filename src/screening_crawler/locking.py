"""File-lock based duplicate-run prevention.

One dataset should never have two crawls acquiring/publishing at once -- a second run
starting while the first is still mid-flight risks two processes racing to write the same
content-addressed artifact path, or (worse) each computing a "previous release" pointer that
the other is concurrently about to change. `flock` on an exclusive, non-blocking advisory
lock is the narrowest reliable mechanism for this on a single host; it does not attempt to
coordinate across hosts (this project's crawler has never run as a distributed service, and
nothing in this milestone requires it to start doing so).
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class RunAlreadyInProgress(Exception):
    """Another process already holds the lock for this source."""


@contextmanager
def run_lock(lock_dir: Path, source_key: str) -> Iterator[None]:
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{source_key}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunAlreadyInProgress(
                f"a crawl for {source_key!r} is already in progress "
                f"(lock held: {lock_path})"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
