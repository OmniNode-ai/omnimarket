# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bind the ledger-guard refusal actor's ports on the writer's machine (OMN-21959).

The names come from ``refused_request_settlement`` in the bus binding reducer's contract: the environment
variable that points at the ledger, the glob of its archive rolls beside it, and the environment variable of
the root a relative worktree cell is resolved under. A corrected request is not sent from here: it is queued
to the local outbox under its original id, so the next replay sends it and a refusal is reported
like any other.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from importlib import resources
from pathlib import Path

import yaml

from omnimarket.handlers.bus_binding import outbox
from omnimarket.handlers.bus_binding.guard_port_types import GuardPorts
from omnimarket.models.work_ledger_append import ModelWorkLedgerAppendRequest


def settlement_policy() -> dict[str, str]:
    contract = yaml.safe_load(
        resources.files("omnimarket.nodes.node_bus_binding_reducer")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    return {str(k): str(v) for k, v in contract["refused_request_settlement"].items()}


class _LocalGuardPorts:
    def __init__(
        self,
        state_dir: Path,
        ledger_files: list[Path],
        worktree_root: Path | None,
    ) -> None:
        self._state_dir = state_dir
        self._ledger_files = ledger_files
        self._worktree_root = worktree_root
        self._lines: list[str] | None = None
        self._bodies: dict[str, dict[str, object]] = {}

    def _all_lines(self) -> list[str]:
        if self._lines is None:
            lines: list[str] = []
            for path in self._ledger_files:
                try:
                    lines.extend(path.read_text(encoding="utf-8").splitlines())
                except OSError:
                    continue
            self._lines = lines
        return self._lines

    def lane_rows(self, lane: str) -> list[str]:
        cell = f"lane={lane}"
        return [
            line
            for line in self._all_lines()
            if any(part.strip() == cell for part in line.split(" | "))
        ]

    def refused_rows(self, path: str) -> str | None:
        try:
            body = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(body, dict) or not body.get("rows"):
            return None
        rows = str(body["rows"])
        self._bodies[rows] = body
        return rows

    def path_exists(self, path: str) -> bool:
        target = Path(path)
        if not target.is_absolute():
            if self._worktree_root is None:
                # Unknowable here: answer that it exists, so the case stays open instead of re-appending.
                return True
            target = self._worktree_root / target
        return target.exists()

    def append(self, rows: str) -> bool:
        body = self._bodies.get(rows, {})
        try:
            request = ModelWorkLedgerAppendRequest.model_validate(
                {
                    key: value
                    for key, value in body.items()
                    if key not in {"refusal", "reported_at", "settled"}
                }
            )
            outbox.queue_request(self._state_dir, request)
        except (OSError, ValueError):
            return False
        return True


def local_guard_ports(
    state_dir: Path, *, env: Mapping[str, str] | None = None
) -> GuardPorts | None:
    """The actor's ports as the contract names them; None when the ledger is not readable on this machine."""
    environ = os.environ if env is None else env
    policy = settlement_policy()
    ledger = environ.get(policy["ledger_path_env"], "")
    if not ledger or not Path(ledger).is_file():
        return None
    ledger_path = Path(ledger)
    files = [
        ledger_path,
        *sorted(ledger_path.parent.glob(policy["ledger_archive_glob"])),
    ]
    root = environ.get(policy["worktree_root_env"], "")
    ports = _LocalGuardPorts(state_dir, files, Path(root) if root else None)
    return GuardPorts(
        lane_rows=ports.lane_rows,
        refused_rows=ports.refused_rows,
        path_exists=ports.path_exists,
        append=ports.append,
    )
