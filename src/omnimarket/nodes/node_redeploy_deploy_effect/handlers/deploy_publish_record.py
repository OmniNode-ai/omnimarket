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

WHAT THE COMPLETION ARM READS FROM IT (OMN-18143)
    The command arm returns as soon as the publish is recorded, because a real rebuild
    takes about 20 minutes and the runtime abandons a dispatch after 600 s. So the
    record also carries what the rollback decision needs, the command's
    ``rollback_target`` and ``smoke_test``, and the ``rebuild-completed`` arm settles
    it: :meth:`settle` hands the entry back exactly once and marks it settled, so a
    redelivered completion never emits a second rolled-back fact. A settled entry
    stays published, so a replay of its command is still skipped.

    The command is also STAGED before it is published, with the same fields. A
    staged entry does not count as published, so a publish that raised is still
    published on redelivery, but a completion that overtakes the write recording the
    publish can still be settled from it, and that write keeps the settled mark.

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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

_STATE_DIR_ENV = "ONEX_STATE_DIR"
_RECORD_SUBDIR = "redeploy_deploy_effect"
_RECORD_FILENAME = "published_rebuild_commands.json"
# 2 adds the ``staged`` map (OMN-18143); a schema 1 record reads with it empty.
_SCHEMA_VERSION = 2

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
        return str(correlation_id) in self._read_document().published

    def stage(
        self,
        correlation_id: UUID,
        *,
        runtime_lane: str,
        git_ref: str | None,
        rollback_target: str,
        smoke_test: bool,
    ) -> None:
        """Note what a completion of this command will need, BEFORE it is published.

        A staged command does not count as published: :meth:`contains` ignores it, so
        a publish that raises is still published on redelivery (OMN-19377 AC4). It
        exists so that a completion which overtakes :meth:`record` can still be
        settled with the command's rollback target (OMN-18143). Logs, never raises: a
        failed stage costs only that race.
        """
        key = str(correlation_id)
        try:
            with self._locked():
                document = self._read_document()
                document.staged.pop(key, None)
                document.staged[key] = {
                    "runtime_lane": runtime_lane,
                    "git_ref": git_ref,
                    "rollback_target": rollback_target,
                    "smoke_test": smoke_test,
                    "staged_at": datetime.now(UTC).isoformat(),
                }
                self._write(document)
        except OSError as exc:
            logger.error(
                "Could not stage a rebuild command; a completion that arrives before "
                "its publish is recorded will be observed without a rollback decision",
                extra={
                    "correlation_id": key,
                    "path": str(self._path),
                    "error": str(exc),
                },
            )

    def record(
        self,
        correlation_id: UUID,
        *,
        runtime_lane: str,
        git_ref: str | None,
        rollback_target: str,
        smoke_test: bool,
        publish_started_at: datetime,
    ) -> None:
        """Record a published command. Logs, never raises: the publish already happened."""
        key = str(correlation_id)
        try:
            with self._locked():
                document = self._read_document()
                staged = document.staged.pop(key, None)
                busy_at = document.busy.get(key)
                if busy_at is not None and busy_at >= publish_started_at.isoformat():
                    logger.warning(
                        "Not recording a published rebuild command: a busy answer "
                        "for its correlation arrived after the publish started",
                        extra={"correlation_id": key, "busy_at": busy_at},
                    )
                    self._write(document)
                    return
                entry: dict[str, Any] = {
                    "runtime_lane": runtime_lane,
                    "git_ref": git_ref,
                    "rollback_target": rollback_target,
                    "smoke_test": smoke_test,
                    "published_at": datetime.now(UTC).isoformat(),
                }
                # A completion that overtook this write settled the staged entry; the
                # published one keeps that mark, or a redelivered completion would be
                # handed the entry a second time.
                if staged is not None and "settled" in staged:
                    entry["settled"] = staged["settled"]
                    entry["settled_at"] = staged.get("settled_at")
                document.published.pop(key, None)
                document.published[key] = entry
                self._write(document)
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

    def settle(self, correlation_id: UUID, *, outcome: str) -> dict[str, Any] | None:
        """Hand back a published or staged, unsettled entry once, and mark it settled.

        Returns ``None`` for a correlation this effect never published (or has since
        evicted or released), and for one already settled: a redelivered completion.
        A staged entry is settled too, because the completion may overtake the write
        that records the publish. The check and the mark are one locked
        read-modify-write, so two deliveries of one completion cannot both be handed
        the entry. A failed write returns ``None`` and logs: that loses one
        rolled-back fact, where handing the entry back unmarked could emit it twice,
        and the orchestrator terminalises each.
        """
        key = str(correlation_id)
        try:
            with self._locked():
                document = self._read_document()
                for entries in (document.published, document.staged):
                    entry = entries.get(key)
                    if entry is None:
                        continue
                    if "settled" in entry:
                        return None
                    entries[key] = {
                        **entry,
                        "settled": outcome,
                        "settled_at": datetime.now(UTC).isoformat(),
                    }
                    self._write(document)
                    return dict(entry)
                return None
        except OSError as exc:
            logger.error(
                "Could not settle a published rebuild command; its completion is "
                "observed but no rollback is decided for it",
                extra={
                    "correlation_id": key,
                    "path": str(self._path),
                    "error": str(exc),
                },
            )
            return None

    def release_busy(self, correlation_id: UUID) -> None:
        """Forget a correlation the agent answered ``busy``, so a later copy is published."""
        key = str(correlation_id)
        try:
            with self._locked():
                document = self._read_document()
                document.published.pop(key, None)
                document.staged.pop(key, None)
                document.busy.pop(key, None)
                document.busy[key] = datetime.now(UTC).isoformat()
                self._write(document)
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

    def _read_document(self) -> _Document:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _Document()
        except OSError as exc:
            logger.error(
                "Could not read the published rebuild command record; reading it "
                "as empty",
                extra={"path": str(self._path), "error": str(exc)},
            )
            return _Document()
        try:
            document = json.loads(raw)
            published = document["published"]
            busy = document.get("busy", {})
            # Absent from a schema 1 record, written before OMN-18143.
            staged = document.get("staged", {})
            if not all(isinstance(m, dict) for m in (published, busy, staged)):
                raise TypeError("published, busy and staged must be objects")
        except (ValueError, KeyError, TypeError) as exc:
            logger.error(
                "The published rebuild command record is malformed; reading it as "
                "empty",
                extra={"path": str(self._path), "error": str(exc)},
            )
            return _Document()
        return _Document(
            published=dict(published),
            busy={str(k): str(v) for k, v in busy.items()},
            staged=dict(staged),
        )

    def _write(self, document: _Document) -> None:
        for entries in (document.published, document.busy, document.staged):
            while len(entries) > self._bound:
                entries.pop(next(iter(entries)))
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "published": document.published,
            "busy": document.busy,
            "staged": document.staged,
        }
        fd, tmp_name = tempfile.mkstemp(
            dir=self._path.parent, prefix=f".{self._path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, sort_keys=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self._path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise


@dataclass
class _Document:
    """The record file's three maps, keyed by correlation id, oldest first."""

    published: dict[str, dict[str, Any]] = field(default_factory=dict)
    busy: dict[str, str] = field(default_factory=dict)
    staged: dict[str, dict[str, Any]] = field(default_factory=dict)


__all__: list[str] = ["RECORD_BOUND", "DeployPublishRecord"]
