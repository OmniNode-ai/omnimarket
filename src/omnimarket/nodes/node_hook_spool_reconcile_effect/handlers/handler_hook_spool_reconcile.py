# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Carries the hook drainer's dead letters back to its journal, and raises an alarm for every loss (OMN-19998, issue L1-06).

The hook-emit drainer (omniclaude) dead-letters a record into a quarantine directory beside its journal when the broker
refuses its class, and a class it has proven refused is dead-lettered on its first failure. Nothing ever carried those
records back once the refusal ended, so the spool never drained, and a dead letter raised no alarm: only the journal
overflowing did. On the lab host h053 the spool held 118 quarantined and 587 set-aside records, none drained.

This node does two things, both from overlay values. It carries a dead letter back into the journal when its class is not
one the drainer currently refuses (a MOVE, never a delete: the drainer publishes it on its next cycle, and a record the
broker still refuses is dead-lettered again and counted as a new loss). And it raises an alarm for each loss line of the
drainer's loss log that no alarm has covered: a typed failure on the emission journal under the node's own piece, then the
alarm state records the loss lines covered. An alarm that could not be written is raised again on the next run.

Canonical definition-B shape: ``handle(ModelHookSpoolReconcileCommand) -> ModelHookSpoolReconcileResult``.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable, Collection, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from omnimarket.handlers.emission_sink import ChainEmitter, ProtocolEmissionSink
from omnimarket.nodes.node_hook_spool_reconcile_effect.models import (
    ModelHookSpoolDecision,
    ModelHookSpoolReading,
    ModelHookSpoolReconcileCommand,
    ModelHookSpoolReconcileResult,
    ModelSpoolRecord,
)
from omnimarket.nodes.node_hook_spool_reconcile_effect.overlay import (
    NODE_NAME,
    ModelHookSpoolReconcileOverlay,
)

LOSS_FAILURE_CODE = "hook_spool_loss_unalarmed"
STATUS_UNREAD_CODE = "hook_spool_drainer_status_unread"
REASON_SUFFIX = ".reason.json"
REPLAYED_DIRNAME = "replayed"


def decide_spool(
    records: Sequence[ModelSpoolRecord],
    refused_event_types: Collection[str],
    batch_limit: int,
    min_age_seconds: int,
    losses_total: int,
    losses_alarmed: int,
) -> ModelHookSpoolDecision:
    """Which dead letters to carry back, and how many losses no alarm covers. Pure: same input, same decision."""
    refused = frozenset(refused_event_types)
    held_refused = sum(1 for r in records if r.event_type in refused)
    candidates = [r for r in records if r.event_type not in refused]
    held_young = sum(1 for r in candidates if r.age_seconds < min_age_seconds)
    old_enough = sorted(
        (r for r in candidates if r.age_seconds >= min_age_seconds),
        key=lambda r: (r.name, r.location),
    )
    return ModelHookSpoolDecision(
        replay=tuple(old_enough[:batch_limit]),
        held_refused=held_refused,
        held_young=held_young,
        unalarmed_losses=max(0, losses_total - losses_alarmed),
    )


class _SpoolUnreadError(Exception):
    """A spool fact the run needs could not be read; the run replays nothing."""


class HandlerHookSpoolReconcile:
    def __init__(
        self,
        overlay: ModelHookSpoolReconcileOverlay,
        home: Path,
        sink: ProtocolEmissionSink,
        backend: Literal["in_memory", "real"],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._overlay = overlay
        self._home = home
        self._sink = sink
        self._backend: Literal["in_memory", "real"] = backend
        self._clock = clock or (lambda: datetime.now(UTC))

    # -- reads -------------------------------------------------------------------------------------------------------

    def _path(self, relpath: str) -> Path:
        return self._home / relpath

    def _read_json(self, relpath: str) -> object:
        try:
            return json.loads(self._path(relpath).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise _SpoolUnreadError(f"{relpath}: {type(exc).__name__}") from exc

    def _refused_types(self) -> tuple[str, ...]:
        status = self._read_json(self._overlay.spool.drainer_status_relpath)
        refused = (
            status.get("refused_event_types") if isinstance(status, dict) else None
        )
        if not isinstance(refused, list) or not all(
            isinstance(t, str) for t in refused
        ):
            raise _SpoolUnreadError("drainer status holds no refused_event_types list")
        return tuple(refused)

    def _alarmed(self) -> int:
        path = self._path(self._overlay.spool.alarm_state_relpath)
        if not path.exists():
            return 0
        state = self._read_json(self._overlay.spool.alarm_state_relpath)
        value = state.get("alarmed_loss_lines") if isinstance(state, dict) else None
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise _SpoolUnreadError("alarm state holds no alarmed_loss_lines count")
        return value

    def _losses(self) -> int:
        try:
            text = self._path(self._overlay.spool.loss_log_relpath).read_text(
                encoding="utf-8"
            )
        except FileNotFoundError:
            return 0
        except OSError as exc:
            raise _SpoolUnreadError(f"loss log: {type(exc).__name__}") from exc
        return sum(1 for line in text.splitlines() if line.strip())

    def _age_seconds(self, queued_at: object, path: Path, now: datetime) -> int:
        stamp: datetime | None = None
        if isinstance(queued_at, str):
            with contextlib.suppress(ValueError):
                stamp = datetime.fromisoformat(queued_at)
        if stamp is None:
            stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        return max(0, int((now - stamp).total_seconds()))

    def _dead_letters(self, now: datetime) -> tuple[list[ModelSpoolRecord], int]:
        records: list[ModelSpoolRecord] = []
        unreadable = 0
        for relpath in self._overlay.spool.dead_letter_relpaths:
            directory = self._path(relpath)
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.json")):
                if path.name.endswith(REASON_SUFFIX):
                    continue
                try:
                    body = json.loads(path.read_text(encoding="utf-8"))
                    event_type = body["event_type"]
                    if not isinstance(event_type, str) or not event_type:
                        raise ValueError("event_type")
                    age = self._age_seconds(body.get("queued_at"), path, now)
                except (OSError, ValueError, KeyError, TypeError):
                    unreadable += 1
                    continue
                records.append(
                    ModelSpoolRecord(
                        name=path.name,
                        location=relpath,
                        event_type=event_type,
                        age_seconds=age,
                    )
                )
        return records, unreadable

    def _pending(self) -> int:
        directory = self._path(self._overlay.spool.journal_relpath)
        if not directory.is_dir():
            return 0
        return sum(1 for p in directory.glob("*.json") if p.is_file())

    def _read(
        self,
        refused: tuple[str, ...],
        losses_alarmed: int,
        now: datetime,
    ) -> tuple[ModelHookSpoolReading, list[ModelSpoolRecord]]:
        records, unreadable = self._dead_letters(now)
        by_class: dict[str, int] = {}
        for r in records:
            by_class[r.event_type] = by_class.get(r.event_type, 0) + 1
        reading = ModelHookSpoolReading(
            pending=self._pending(),
            dead_lettered=len(records),
            unreadable=unreadable,
            by_class=by_class,
            losses_total=self._losses(),
            losses_alarmed=losses_alarmed,
            refused_event_types=refused,
        )
        return reading, records

    # -- apply -------------------------------------------------------------------------------------------------------

    def _replay(self, records: Sequence[ModelSpoolRecord]) -> int:
        journal = self._path(self._overlay.spool.journal_relpath)
        journal.mkdir(parents=True, exist_ok=True)
        moved = 0
        for record in records:
            source_dir = self._path(record.location)
            target = journal / record.name
            if target.exists():
                continue
            try:
                (source_dir / record.name).replace(target)
            except OSError:
                continue
            moved += 1
            reason = source_dir / f"{Path(record.name).stem}{REASON_SUFFIX}"
            if reason.exists():
                with contextlib.suppress(OSError):
                    done = source_dir / REPLAYED_DIRNAME
                    done.mkdir(exist_ok=True)
                    reason.replace(done / reason.name)
        return moved

    def _write_alarm_state(self, losses_total: int) -> None:
        path = self._path(self._overlay.spool.alarm_state_relpath)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(
            json.dumps({"alarmed_loss_lines": losses_total}), encoding="utf-8"
        )
        tmp.replace(path)

    # -- the run -----------------------------------------------------------------------------------------------------

    def handle(
        self, command: ModelHookSpoolReconcileCommand
    ) -> ModelHookSpoolReconcileResult:
        now = self._clock()
        chain = ChainEmitter(
            self._sink,
            node=NODE_NAME,
            piece=self._overlay.emission.piece,
            origin="probe",
            clock=self._clock,
            backend=self._backend,
        )
        chain.start()
        empty = ModelHookSpoolReading(
            pending=0,
            dead_lettered=0,
            unreadable=0,
            by_class={},
            losses_total=0,
            losses_alarmed=0,
            refused_event_types=(),
        )
        try:
            refused = self._refused_types()
            alarmed = self._alarmed()
            before, records = self._read(refused, alarmed, now)
        except _SpoolUnreadError as exc:
            # An unreadable spool fact is never an empty answer: nothing is carried back, and the chain fails typed.
            chain.failure(STATUS_UNREAD_CODE, str(exc))
            return ModelHookSpoolReconcileResult(
                ok=False,
                status="unread",
                before=empty,
                after=empty,
                replayed=0,
                held_refused=0,
                held_young=0,
                alarmed_losses=0,
                reason=str(exc),
            )
        decision = decide_spool(
            records,
            refused,
            self._overlay.replay.batch_limit,
            self._overlay.replay.min_age_seconds,
            before.losses_total,
            before.losses_alarmed,
        )
        replayed = self._replay(decision.replay) if command.replay else 0
        after, _ = self._read(refused, alarmed, now)
        if decision.unalarmed_losses > 0:
            classes = ",".join(sorted(before.by_class)) or "none"
            chain.failure(
                LOSS_FAILURE_CODE,
                f"{decision.unalarmed_losses} hook events lost and no alarm covered them; "
                f"dead letters by class: {classes}",
            )
            # After the alarm is on the journal, never before: an alarm that fails to record is raised again next run.
            self._write_alarm_state(before.losses_total)
        else:
            chain.success(
                counters={
                    "pending": after.pending,
                    "dead_lettered": after.dead_lettered,
                    "replayed": replayed,
                    "losses": before.losses_total,
                    "unreadable": after.unreadable,
                }
            )
        drained = after.pending == 0 and after.dead_lettered == 0
        status: Literal["drained", "draining", "stuck", "unread"] = (
            "drained" if drained else "draining" if replayed > 0 else "stuck"
        )
        return ModelHookSpoolReconcileResult(
            ok=decision.unalarmed_losses == 0,
            status=status,
            before=before,
            after=after,
            replayed=replayed,
            held_refused=decision.held_refused,
            held_young=decision.held_young,
            alarmed_losses=decision.unalarmed_losses,
        )
