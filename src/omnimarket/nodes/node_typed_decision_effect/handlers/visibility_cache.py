# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A positive-only, time-limited memory of repository-visibility reads (OMN-20149).

The typed-decision node reads a repository's visibility before anything reaches
the third-party decision backend. GitHub caps an anonymous read at 60 an hour
per address, so a burst of decisions turned into refusals that read as model
failures. This cache keeps the one answer that is safe to keep: a real read that
reported a repository public, for a short TTL.

What it never keeps, by construction: a refusal, a 404, a rate limit, a
transport failure, or a repository reported private. The only method that writes
is :meth:`record_public`, and the handler calls it only after a 200 whose
``private`` field is false. A cache miss therefore always falls through to the
live read, and the guard stays closed.

The file form exists because the node runs as one process per CLI call, so an
in-process dict alone would never be hit. It holds repository slugs and the time
of the read, nothing else: no token, no response body. A corrupt, unreadable or
future-dated file is ignored, never trusted, and a write failure never blocks a
decision.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

_FORMAT_VERSION = 1


class VisibilityCache:
    """Remember, for ``ttl_seconds``, which repositories a live read found public."""

    def __init__(
        self,
        *,
        ttl_seconds: int,
        clock: Callable[[], float],
        path: Path | None = None,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._path = path
        self._checked_at: dict[str, float] = {}

    @staticmethod
    def _key(repository: str) -> str:
        # GitHub slugs are case-insensitive.
        return repository.lower()

    def _fresh(self, checked_at: float) -> bool:
        age = self._clock() - checked_at
        # A negative age is a future-dated entry: a clock that ran ahead, or a
        # tampered file. It must not extend a stale answer.
        return 0 <= age < self._ttl

    def is_public(self, repository: str) -> bool:
        """True only when a live read found ``repository`` public inside the TTL."""
        if self._ttl <= 0:
            return False
        key = self._key(repository)
        seen = self._checked_at.get(key)
        if seen is not None and self._fresh(seen):
            return True
        # Another process may have read it since: look at the file.
        seen = self._read_file().get(key)
        if seen is not None and self._fresh(seen):
            self._checked_at[key] = seen
            return True
        return False

    def record_public(self, repository: str) -> None:
        """Remember a live 200 that reported ``repository`` public. Nothing else."""
        if self._ttl <= 0:
            return
        now = self._clock()
        self._checked_at[self._key(repository)] = now
        if self._path is None:
            return
        merged = {
            key: seen
            for key, seen in {**self._read_file(), **self._checked_at}.items()
            if self._fresh(seen)
        }
        self._write_file(merged)

    # ------------------------------------------------------------------ file

    def _read_file(self) -> dict[str, float]:
        if self._path is None:
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict) or raw.get("version") != _FORMAT_VERSION:
            return {}
        entries = raw.get("public")
        if not isinstance(entries, dict):
            return {}
        return {
            key: float(seen)
            for key, seen in entries.items()
            if isinstance(key, str)
            and isinstance(seen, int | float)
            and not isinstance(seen, bool)
        }

    def _write_file(self, entries: dict[str, float]) -> None:
        if self._path is None:
            return
        payload = json.dumps({"version": _FORMAT_VERSION, "public": entries})
        tmp_name: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=self._path.parent, prefix=f".{self._path.name}."
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, self._path)
            tmp_name = None
        except OSError:
            # A cache that cannot persist is a cache miss next time, never a
            # failed decision.
            pass
        finally:
            if tmp_name is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)


__all__: list[str] = ["VisibilityCache"]
