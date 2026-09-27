# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Durable record of the rebuild commands the deploy effect has published.

WHY IT IS A FILE AND NOT PROCESS MEMORY
    The deploy effect waits on a rebuild that recreates the effect's own container,
    and the runtime quarantines any dispatch that runs past its deadline, after which
    ``node_dlq_replay_effect`` puts the same deploy-publish record back on the topic.
    Both hand the same correlation to a handler whose process memory is empty or
    belongs to another instance. A record under ``ONEX_STATE_DIR`` survives both: on
    every lane that directory is a named volume that outlives the container.

WHAT IS RECORDED, AND WHEN
    A correlation is recorded after its rebuild command's publish returns, and never
    before, so a publish that raised leaves nothing behind and its redelivery is
    published. It is removed when the agent answers ``busy``, the one answer for which
    the agent keeps no job. A crash between the publish and the write can let one
    copy through; the agent refuses that copy as ``duplicate``.

    A ``busy`` also leaves a tombstone with its time. A record whose publish started
    before the latest tombstone is not written, because that ``busy`` may have
    answered it: the effect's rejection arm can see the answer before the publishing
    handler resumes. Skipping the write fails open, to one more publish.

WHAT A BROKEN FILE DOES
    It fails open. An unreadable record reads as empty and a failed write is logged at
    error level. Either way the worst outcome is one more publish that the agent
    refuses as ``duplicate``, never a real deploy that is silently dropped.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

_STATE_DIR_ENV = "ONEX_STATE_DIR"
_RECORD_SUBDIR = "redeploy_deploy_effect"
_RECORD_FILENAME = "published_rebuild_commands.json"
_SCHEMA_VERSION = 1

# How many published correlations the record keeps. A repeat arrives within minutes
# to hours of the original (the replay cadence measured on the dev lane is ten
# minutes), never thousands of deploys later, so a bounded window covers it.
RECORD_BOUND = 4096


class DeployPublishRecord:
    """The correlations whose rebuild command reached the broker, kept on disk."""

    def __init__(self, path: Path, *, bound: int = RECORD_BOUND) -> None:
        self._path = path
        self._lock_path = path.with_suffix(".lock")
        self._bound = bound

    @classmethod
    def from_env(cls) -> DeployPublishRecord:
        """Resolve the record under ``ONEX_STATE_DIR``, failing loud when it is unset.

        There is no in-memory fallback: a deploy effect without a durable record is
        the defect this record exists to remove, and it would look healthy.
        """
        state_dir = os.environ.get(_STATE_DIR_ENV, "").strip()
        if not state_dir:
            raise RuntimeError(  # error-ok: mis-wired runtime environment
                f"{_STATE_DIR_ENV} is not set; the deploy effect keeps its record of "
                "published rebuild commands under it so that a redelivered or "
                "replayed command is not published again after a restart"
            )
        return cls(Path(state_dir) / _RECORD_SUBDIR / _RECORD_FILENAME)

    @property
    def path(self) -> Path:
        """Where the record lives."""
        return self._path

    def contains(self, correlation_id: UUID) -> bool:
        """Whether a rebuild command for this correlation has been published."""
        return str(correlation_id) in self._read()

    def record(
        self,
        correlation_id: UUID,
        *,
        runtime_lane: str,
        git_ref: str | None,
        publish_started_at: datetime,
    ) -> None:
        """Record a published command. Logs, never raises: the publish already happened."""
        key = str(correlation_id)
        try:
            with self._locked():
                published, busy = self._read_document()
                busy_at = busy.get(key)
                if busy_at is not None and busy_at >= publish_started_at.isoformat():
                    logger.warning(
                        "Not recording a published rebuild command: a busy answer "
                        "for its correlation arrived after the publish started",
                        extra={"correlation_id": key, "busy_at": busy_at},
                    )
                    return
                published.pop(key, None)
                published[key] = {
                    "runtime_lane": runtime_lane,
                    "git_ref": git_ref,
                    "published_at": datetime.now(UTC).isoformat(),
                }
                self._write(published, busy)
        except OSError as exc:
            logger.error(
                "Could not record a published rebuild command; a redelivery of it "
                "will be published again and refused by the agent as duplicate",
                extra={
                    "correlation_id": str(correlation_id),
                    "path": str(self._path),
                    "error": str(exc),
                },
            )

    def release_busy(self, correlation_id: UUID) -> None:
        """Forget a correlation the agent answered ``busy``, so a later copy is published."""
        key = str(correlation_id)
        try:
            with self._locked():
                published, busy = self._read_document()
                published.pop(key, None)
                busy.pop(key, None)
                busy[key] = datetime.now(UTC).isoformat()
                self._write(published, busy)
        except OSError as exc:
            logger.error(
                "Could not release a published rebuild command; a later copy of it "
                "will be skipped instead of published",
                extra={
                    "correlation_id": str(correlation_id),
                    "path": str(self._path),
                    "error": str(exc),
                },
            )

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)

    def _read(self) -> dict[str, dict[str, Any]]:
        return self._read_document()[0]

    def _read_document(self) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}, {}
        except OSError as exc:
            logger.error(
                "Could not read the published rebuild command record; reading it "
                "as empty",
                extra={"path": str(self._path), "error": str(exc)},
            )
            return {}, {}
        try:
            document = json.loads(raw)
            published = document["published"]
            busy = document.get("busy", {})
            if not isinstance(published, dict) or not isinstance(busy, dict):
                raise TypeError("published and busy must be objects")
        except (ValueError, KeyError, TypeError) as exc:
            logger.error(
                "The published rebuild command record is malformed; reading it as "
                "empty",
                extra={"path": str(self._path), "error": str(exc)},
            )
            return {}, {}
        return dict(published), {str(k): str(v) for k, v in busy.items()}

    def _write(
        self, published: dict[str, dict[str, Any]], busy: dict[str, str]
    ) -> None:
        while len(published) > self._bound:
            published.pop(next(iter(published)))
        while len(busy) > self._bound:
            busy.pop(next(iter(busy)))
        document = {
            "schema_version": _SCHEMA_VERSION,
            "published": published,
            "busy": busy,
        }
        fd, tmp_name = tempfile.mkstemp(
            dir=self._path.parent, prefix=f".{self._path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(document, fh, sort_keys=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self._path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise


__all__: list[str] = ["RECORD_BOUND", "DeployPublishRecord"]
