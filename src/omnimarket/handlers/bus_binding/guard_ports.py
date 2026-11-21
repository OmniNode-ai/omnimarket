# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bind the ledger-guard refusal actor's ports on the writer's machine (OMN-21959).

The names come from ``refused_request_settlement`` in the bus binding reducer's contract: the environment
variable of the root a relative worktree cell is resolved under. The lane's ledger rows are read through the
ledger rows reader port (the bus-fed projection), never from a ledger file; a ledger that cannot be read
raises ``LedgerReadError`` out of ``lane_rows``, which the actor treats as unknown. A corrected request is not
sent from here: it is queued to the local outbox as a new request, so the replay that runs next sends it and a
refusal of it is reported like any other.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

import yaml

from omnimarket.handlers.adapter_work_ledger_rows_reader import (
    SubprocessLedgerRowsReader,
)
from omnimarket.handlers.bus_binding import outbox
from omnimarket.handlers.bus_binding.guard_port_types import GuardPorts
from omnimarket.models.work_ledger_append import ModelWorkLedgerAppendRequest
from omnimarket.protocols.protocol_ledger_rows_reader import ProtocolLedgerRowsReader

#: The reader node's name for the lane-rows read, and the window it covers. No horizon is declared by the
#: actor, so the window is a week: a refusal older than that finds no CLAIM and stays open for the operator.
LANE_ROWS_READER = "bus-binding-guard-lane-rows"
LANE_ROWS_WINDOW_HOURS = 168


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
        ledger_reader: ProtocolLedgerRowsReader,
        worktree_root: Path | None,
    ) -> None:
        self._state_dir = state_dir
        self._ledger_reader = ledger_reader
        self._worktree_root = worktree_root
        self._lines: tuple[str, ...] | None = None
        self._bodies: dict[str, dict[str, object]] = {}

    def _all_lines(self) -> tuple[str, ...]:
        """The window's rows, read once; raises LedgerReadError, and an unread ledger is never cached as empty."""
        if self._lines is None:
            self._lines = self._ledger_reader.rows(
                reader=LANE_ROWS_READER, window_hours=LANE_ROWS_WINDOW_HOURS
            )
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
            request = ModelWorkLedgerAppendRequest(
                request_id=uuid.uuid4(),
                ledger_id=str(body.get("ledger_id") or "rolling-work-ledger"),
                rows=rows,
                requested_by_lane=str(body.get("requested_by_lane") or "unknown"),
                requesting_host=str(body.get("requesting_host") or "unknown"),
                requested_at=datetime.now(tz=UTC),
            )
            outbox.queue_request(self._state_dir, request)
        except (OSError, ValueError):
            return False
        return True


def local_guard_ports(
    state_dir: Path,
    *,
    env: Mapping[str, str] | None = None,
    ledger_reader: ProtocolLedgerRowsReader | None = None,
) -> GuardPorts:
    """The actor's ports as the contract names them; the ledger is read through the bus reader, lazily.

    A ledger that cannot be read raises ``LedgerReadError`` from ``lane_rows`` when the actor first asks.
    """
    environ = os.environ if env is None else env
    policy = settlement_policy()
    root = environ.get(policy["worktree_root_env"], "")
    ports = _LocalGuardPorts(
        state_dir,
        ledger_reader or SubprocessLedgerRowsReader(environ),
        Path(root) if root else None,
    )
    return GuardPorts(
        lane_rows=ports.lane_rows,
        refused_rows=ports.refused_rows,
        path_exists=ports.path_exists,
        append=ports.append,
    )
