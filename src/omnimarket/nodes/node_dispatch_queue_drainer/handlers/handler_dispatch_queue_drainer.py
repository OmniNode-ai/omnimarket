# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Drainer for legacy dispatch queue YAML files, with a durable progress operator.

OMN-17018 (off-rails rev 2 §3 RC-A / §4 A10 step 4). Before this the drainer
selected the oldest queue item, compiled it, wrote a result artifact and left
the file byte-identical, so every subsequent run re-selected the *same* item
forever: the queue had no progress operator and "compiled" stood in for
"executed". Selection is now over items that are still ``QUEUED``, and the
selected item is provably transitioned QUEUED -> CLAIMED -> DISPATCHED (or
-> TERMINAL with a typed stop reason) before the run returns.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml
from pydantic import ValidationError

from omnimarket.enums.enum_dispatch_queue_phase import EnumDispatchQueuePhase
from omnimarket.enums.enum_dispatch_terminal_reason import EnumDispatchTerminalReason
from omnimarket.nodes.node_dispatch_queue_drainer.handlers.dispatch_queue_lifecycle_ledger import (
    LIFECYCLE_DIRNAME,
    FileDispatchQueueLifecycleLedger,
    ProtocolDispatchQueueLifecycleLedger,
    blocked_terminal,
)
from omnimarket.nodes.node_dispatch_queue_drainer.models import (
    ModelDispatchQueueDrainerRequest,
    ModelDispatchQueueDrainerResult,
    ModelDispatchQueueItem,
    ModelDispatchQueueLifecycle,
)
from omnimarket.nodes.node_dispatch_worker import (
    ModelDispatchWorkerCommand,
    ModelDispatchWorkerResult,
)


class UnreadableQueueItemError(RuntimeError):
    """A queue item could not be read or validated into a typed item.

    Carries the terminal reason the item will be closed with, so the stop is
    classified where it is detected rather than re-derived from a message.
    """

    def __init__(self, message: str, *, reason: EnumDispatchTerminalReason) -> None:
        super().__init__(message)
        self.reason = reason


@runtime_checkable
class ProtocolDispatchWorker(Protocol):
    """Protocol for a handler that compiles a dispatch worker command."""

    def handle(
        self,
        command: ModelDispatchWorkerCommand,
        *,
        tasks_dir: Path | None = None,
        existing_task_subjects: list[str] | None = None,
        state_dir: Path | str | None = None,
        parent_session_id: str | None = None,
    ) -> ModelDispatchWorkerResult: ...


class HandlerDispatchQueueDrainer:
    """Read one QUEUED item, compile it, and durably advance its lifecycle."""

    def __init__(
        self,
        dispatch_worker: ProtocolDispatchWorker | None = None,
        lifecycle_ledger: ProtocolDispatchQueueLifecycleLedger | None = None,
    ) -> None:
        if dispatch_worker is not None:
            self._dispatch_worker: ProtocolDispatchWorker = dispatch_worker
        else:
            from omnimarket.nodes.node_dispatch_worker.handlers.handler_dispatch_worker import (
                HandlerDispatchWorker,
            )

            self._dispatch_worker = HandlerDispatchWorker()
        self._injected_ledger = lifecycle_ledger

    def handle(
        self, payload: ModelDispatchQueueDrainerRequest
    ) -> ModelDispatchQueueDrainerResult:
        """Compile one queue item, transition it, and persist a result artifact.

        ``limit`` is validated on ``payload`` at construction time (fail-fast,
        OMN-14242) rather than as a late guard here — an out-of-range limit
        never reaches this method because the request model rejects it.
        """
        resolved_state_dir = _resolve_state_dir(payload.state_dir)
        resolved_queue_dir = payload.queue_dir or resolved_state_dir / "dispatch_queue"
        ledger = self._resolve_ledger(resolved_state_dir)
        now = datetime.now(tz=UTC)

        selected_path = payload.queue_item_path or self._oldest_selectable_item(
            resolved_queue_dir, ledger
        )
        if selected_path is None:
            return self._finish(
                ModelDispatchQueueDrainerResult(
                    status="empty", dry_run=payload.dry_run
                ),
                resolved_state_dir,
                payload,
            )

        existing = ledger.load(selected_path)
        if existing is not None and existing.phase is not EnumDispatchQueuePhase.QUEUED:
            # An explicitly-named item that some attempt already holds or closed.
            # Never re-processed as untouched: the run reports where it actually is.
            return self._finish(
                ModelDispatchQueueDrainerResult(
                    status="blocked",
                    queue_item_path=str(selected_path),
                    blocked_reason=(
                        f"queue item is already at lifecycle phase "
                        f"{existing.phase.value!r}; it is not selectable"
                    ),
                    lifecycle_phase=existing.phase,
                    lifecycle_record_path=str(ledger.record_path(selected_path)),
                    terminal_disposition=(
                        existing.terminal.disposition
                        if existing.terminal is not None
                        else None
                    ),
                    terminal_reason=(
                        existing.terminal.reason
                        if existing.terminal is not None
                        else None
                    ),
                    dry_run=payload.dry_run,
                ),
                resolved_state_dir,
                payload,
            )

        try:
            item = self._load_item(selected_path)
        except UnreadableQueueItemError as exc:
            return self._terminate(
                selected_path,
                ledger=ledger,
                payload=payload,
                state_dir=resolved_state_dir,
                reason=exc.reason,
                message=str(exc),
                now=now,
                command=None,
                worker_result=None,
            )

        command = _to_dispatch_worker_command(item)
        missing_repo_reason = self._missing_repo_reason(
            item, omni_home=payload.omni_home
        )
        if missing_repo_reason:
            return self._terminate(
                selected_path,
                ledger=ledger,
                payload=payload,
                state_dir=resolved_state_dir,
                reason=EnumDispatchTerminalReason.DEPENDENCY_FAILURE,
                message=missing_repo_reason,
                now=now,
                command=command,
                worker_result=None,
            )

        if payload.dry_run:
            # A real dry run: nothing is claimed, dispatched, recorded or written.
            return self._finish(
                ModelDispatchQueueDrainerResult(
                    status="dry_run",
                    queue_item_path=str(selected_path),
                    dispatch_worker_command=command.model_dump(mode="json"),
                    lifecycle_record_path=str(ledger.record_path(selected_path)),
                    dry_run=True,
                ),
                resolved_state_dir,
                payload,
            )

        ledger.claim(
            selected_path,
            actor=payload.actor,
            lease_seconds=payload.claim_lease_seconds,
            now=now,
        )

        compiled = self._dispatch_worker.handle(
            command, tasks_dir=payload.tasks_dir, state_dir=resolved_state_dir
        )

        if compiled.rejected_reason:
            return self._terminate(
                selected_path,
                ledger=ledger,
                payload=payload,
                state_dir=resolved_state_dir,
                reason=EnumDispatchTerminalReason.DEPENDENCY_FAILURE,
                message=f"dispatch worker rejected: {compiled.rejected_reason}",
                now=now,
                command=command,
                worker_result=compiled,
            )

        lifecycle = ledger.mark_dispatched(
            selected_path,
            actor=payload.actor,
            ack_timeout_seconds=payload.dispatch_ack_timeout_seconds,
            now=now,
            detail="dispatch command compiled and handed off",
        )
        return self._finish(
            ModelDispatchQueueDrainerResult(
                status="compiled",
                queue_item_path=str(selected_path),
                dispatch_worker_command=command.model_dump(mode="json"),
                dispatch_worker_result=compiled.model_dump(mode="json"),
                lifecycle_phase=lifecycle.phase,
                lifecycle_record_path=str(ledger.record_path(selected_path)),
            ),
            resolved_state_dir,
            payload,
        )

    def _resolve_ledger(self, state_dir: Path) -> ProtocolDispatchQueueLifecycleLedger:
        if self._injected_ledger is not None:
            return self._injected_ledger
        return FileDispatchQueueLifecycleLedger(
            state_dir / "dispatch_queue" / LIFECYCLE_DIRNAME
        )

    def _load_item(self, path: Path) -> ModelDispatchQueueItem:
        """Parse one queue item, or raise the typed stop it could not be parsed for.

        A queue item that cannot be read or validated is closed as ``unknown``:
        the stop is unclassifiable from the record alone, and ``unknown`` is
        non-redispatchable by construction, so it escalates instead of looping.
        """
        try:
            raw = self._read_yaml(path)
        except (OSError, yaml.YAMLError) as exc:
            raise UnreadableQueueItemError(
                f"queue item could not be read: {exc!s}",
                reason=EnumDispatchTerminalReason.UNKNOWN,
            ) from exc
        if not isinstance(raw, dict):
            raise UnreadableQueueItemError(
                "queue item YAML must contain a mapping",
                reason=EnumDispatchTerminalReason.UNKNOWN,
            )
        try:
            return ModelDispatchQueueItem.model_validate(raw)
        except ValidationError as exc:
            raise UnreadableQueueItemError(
                f"invalid queue item: {exc.errors()[0]['msg']}",
                reason=EnumDispatchTerminalReason.UNKNOWN,
            ) from exc

    def _terminate(
        self,
        selected_path: Path,
        *,
        ledger: ProtocolDispatchQueueLifecycleLedger,
        payload: ModelDispatchQueueDrainerRequest,
        state_dir: Path,
        reason: EnumDispatchTerminalReason,
        message: str,
        now: datetime,
        command: ModelDispatchWorkerCommand | None,
        worker_result: ModelDispatchWorkerResult | None,
    ) -> ModelDispatchQueueDrainerResult:
        """Close the item as STOPPED with *reason* and build the blocked result."""
        terminal = blocked_terminal(reason)
        lifecycle: ModelDispatchQueueLifecycle | None = None
        if not payload.dry_run:
            lifecycle = ledger.mark_terminal(
                selected_path,
                actor=payload.actor,
                terminal=terminal,
                now=now,
                detail=message,
            )
        return self._finish(
            ModelDispatchQueueDrainerResult(
                status="blocked",
                queue_item_path=str(selected_path),
                blocked_reason=message,
                dispatch_worker_command=(
                    None if command is None else command.model_dump(mode="json")
                ),
                dispatch_worker_result=(
                    None
                    if worker_result is None
                    else worker_result.model_dump(mode="json")
                ),
                lifecycle_phase=None if lifecycle is None else lifecycle.phase,
                lifecycle_record_path=str(ledger.record_path(selected_path)),
                terminal_disposition=terminal.disposition,
                terminal_reason=terminal.reason,
                dry_run=payload.dry_run,
            ),
            state_dir,
            payload,
        )

    def _read_yaml(self, path: Path) -> Any:
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def _oldest_selectable_item(
        self, queue_dir: Path, ledger: ProtocolDispatchQueueLifecycleLedger
    ) -> Path | None:
        """Oldest item that is still ``QUEUED``.

        The progress operator lives here: an item that any attempt has claimed,
        dispatched, started or closed is skipped, so N successive runs drain N
        distinct items instead of re-selecting the same one forever.
        """
        if not queue_dir.exists():
            return None
        candidates = sorted(
            (
                path
                for path in queue_dir.glob("*.yaml")
                if path.is_file() and path.parent == queue_dir
            ),
            key=lambda path: (path.stat().st_mtime, path.name),
        )
        for path in candidates:
            lifecycle = ledger.load(path)
            if lifecycle is None or lifecycle.phase is EnumDispatchQueuePhase.QUEUED:
                return path
        return None

    def _missing_repo_reason(
        self, item: ModelDispatchQueueItem, *, omni_home: Path | None
    ) -> str:
        repo = item.resolved_repo
        if not repo:
            return "queue item does not declare a repo and no repo target could be inferred"
        root = omni_home or _resolve_omni_home()
        repo_path = root / repo
        if not repo_path.is_dir():
            return f"repo {repo!r} not found under {root}"
        return ""

    def _finish(
        self,
        result: ModelDispatchQueueDrainerResult,
        state_dir: Path,
        payload: ModelDispatchQueueDrainerRequest,
    ) -> ModelDispatchQueueDrainerResult:
        """Persist the result artifact unless this run is a dry run."""
        if payload.dry_run:
            return result
        return self._write_result(result, state_dir)

    def _write_result(
        self, result: ModelDispatchQueueDrainerResult, state_dir: Path
    ) -> ModelDispatchQueueDrainerResult:
        artifact_dir = state_dir / "dispatch_queue" / "drainer_results"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        stem = "empty"
        if result.queue_item_path:
            stem = Path(result.queue_item_path).stem
        out_path = artifact_dir / f"{stem}-result.json"
        payload = result.model_copy(update={"result_artifact_path": str(out_path)})
        out_path.write_text(
            json.dumps(payload.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
        return payload


def _resolve_state_dir(state_dir: Path | None) -> Path:
    if state_dir is not None:
        return state_dir
    raw_state_dir = os.environ.get("ONEX_STATE_DIR")
    if raw_state_dir:
        return Path(raw_state_dir)
    return _resolve_omni_home() / ".onex_state"


def _resolve_omni_home() -> Path:
    return Path(os.environ["OMNI_HOME"])


def _to_dispatch_worker_command(
    item: ModelDispatchQueueItem,
) -> ModelDispatchWorkerCommand:
    return ModelDispatchWorkerCommand(
        name=item.name,
        team=item.team,
        role=item.role,
        scope=item.scope,
        targets=item.targets,
        collision_fences=item.collision_fences,
        reports_to=item.reports_to,
        wall_clock_cap_min=item.wall_clock_cap_min,
        model=item.model,
        replace=item.replace,
    )


__all__: list[str] = [
    "HandlerDispatchQueueDrainer",
    "ProtocolDispatchWorker",
    "UnreadableQueueItemError",
]
