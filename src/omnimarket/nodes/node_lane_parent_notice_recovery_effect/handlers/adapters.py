# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory and real adapters of the parent notice recovery ports (OMN-23389)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

from omnimarket.nodes.node_lab_host_repair_dispatch_effect.handlers.adapters import (
    Runner,
    fill,
    run_command,
)
from omnimarket.nodes.node_lane_parent_notice_recovery_effect.models.model_notice_recovery import (
    ModelNoticeState,
)


class RecoveryError(RuntimeError):
    """The recovery lane or a ledger row was not started."""


class InMemoryLedgerLines:
    def __init__(self, lines: list[str] | None = None) -> None:
        self.lines = lines if lines is not None else []

    def read_lines(self) -> list[str]:
        return list(self.lines)


class FileLedgerLines:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read_lines(self) -> list[str]:
        return self.path.read_text(encoding="utf-8").splitlines()


class InMemoryNoticeStateStore:
    def __init__(self) -> None:
        self.state = ModelNoticeState()
        self.saves = 0

    def load(self) -> ModelNoticeState:
        return self.state

    def save(self, state: ModelNoticeState) -> None:
        self.state = state
        self.saves += 1


class JsonFileNoticeStateStore:
    """Durable state: one JSON document, fsynced and replaced atomically on every save."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> ModelNoticeState:
        if not self.path.exists():
            return ModelNoticeState()
        return ModelNoticeState.model_validate_json(
            self.path.read_text(encoding="utf-8")
        )

    def save(self, state: ModelNoticeState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            dir=self.path.parent, prefix=".notice-state-"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(state.model_dump_json(indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise


class InMemoryReceipts:
    def __init__(
        self, receipts: Mapping[str, Mapping[str, object]] | None = None
    ) -> None:
        self.receipts = dict(receipts or {})

    def read(self, path: str) -> Mapping[str, object] | None:
        return self.receipts.get(path)


class FileReceipts:
    def read(self, path: str) -> Mapping[str, object] | None:
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return document if isinstance(document, dict) else None


class InMemoryRecoveryDispatcher:
    def __init__(self, refuse: bool = False) -> None:
        self.refuse = refuse
        self.lanes: list[tuple[str, str, str]] = []
        self.ledgers: list[str] = []

    def dispatch(
        self,
        lane: str,
        original: str,
        brief_file: str,
        ticket: str,
        parent: str,
        model: str,
        ledger: str,
    ) -> None:
        if self.refuse:
            raise RecoveryError(f"{lane}: refused")
        self.lanes.append((lane, original, brief_file))
        self.ledgers.append(ledger)


class ArgvRecoveryDispatcher:
    def __init__(
        self, argv: tuple[str, ...], timeout: int, runner: Runner = run_command
    ) -> None:
        self.argv, self.timeout, self.runner = argv, timeout, runner

    def dispatch(
        self,
        lane: str,
        original: str,
        brief_file: str,
        ticket: str,
        parent: str,
        model: str,
        ledger: str,
    ) -> None:
        command = fill(
            self.argv,
            lane=lane,
            original=original,
            brief_file=brief_file,
            ticket=ticket,
            parent=parent,
            model=model,
            ledger=ledger,
        )
        try:
            done = self.runner(command, self.timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RecoveryError(f"{lane}: {type(exc).__name__}") from None
        if done.returncode != 0:
            raise RecoveryError(f"{lane}: recovery exit {done.returncode}")


class InMemoryNoticeSink:
    def __init__(self) -> None:
        self.acks: list[tuple[str, str, str]] = []
        self.escalations: list[str] = []

    def acknowledge(self, lane: str, notice: str, action: str) -> None:
        self.acks.append((lane, notice, action))

    def escalate(self, row: str) -> None:
        self.escalations.append(row)


class ArgvNoticeSink:
    def __init__(
        self,
        ack_argv: tuple[str, ...],
        escalate_argv: tuple[str, ...],
        timeout: int,
        runner: Runner = run_command,
    ) -> None:
        self.ack_argv, self.escalate_argv = ack_argv, escalate_argv
        self.timeout, self.runner = timeout, runner

    def acknowledge(self, lane: str, notice: str, action: str) -> None:
        done = self.runner(
            fill(self.ack_argv, lane=lane, notice=notice, action=action), self.timeout
        )
        if done.returncode != 0:
            raise RecoveryError(
                f"acknowledgement of {notice} refused (exit {done.returncode})"
            )

    def escalate(self, row: str) -> None:
        done = self.runner(fill(self.escalate_argv, row=row), self.timeout)
        if done.returncode != 0:
            raise RecoveryError(f"escalation row refused (exit {done.returncode})")
