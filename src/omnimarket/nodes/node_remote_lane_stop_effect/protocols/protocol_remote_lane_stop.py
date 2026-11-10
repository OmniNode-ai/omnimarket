# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ports the remote-lane stop handler acts through.

Every port raises ``RemoteLaneStopPortError`` when it cannot run; the handler turns that into
a ``refused`` result and writes no ledger row.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from omnimarket.nodes.node_remote_lane_stop_effect.models.model_remote_lane_stop import (
    ModelStopHost,
)


class RemoteLaneStopPortError(RuntimeError):
    """A port could not run; the message names what failed."""


@dataclass(frozen=True)
class StopLedgerWrite:
    """The ledger writer's answer: accepted with the row's timestamp, or refused with its words."""

    accepted: bool
    ts: str | None
    output: str


class ProtocolStopTransport(Protocol):
    """Signals a lane's process tree and reads whether it is still there."""

    def signal_lab(self, host: ModelStopHost, run_id: str, sig: str) -> bool: ...

    def lab_tree_alive(self, host: ModelStopHost, run_id: str) -> str | None: ...

    def reap_lab_unit(self, host: ModelStopHost, unit: str) -> None: ...

    def signal_local(self, pid: int, sig: str) -> None: ...

    def local_alive(self, pid: int) -> bool: ...

    def remove_slot_markers(
        self, placement_dir: str, host: str, pid: int
    ) -> list[str]: ...


class ProtocolStopCloud(Protocol):
    """Ends a cloud session: the archive call, then a read of whether it has ended."""

    def archive(self, session_id: str, account: str) -> None: ...

    def session_ended(self, session_id: str, account: str) -> bool: ...


class ProtocolStopLedger(Protocol):
    """Reads the rolling ledger and writes one TERMINAL through the ledger writer."""

    def read_text(self, path: str) -> str: ...

    def write_terminal(
        self, command: list[str], path: str, cells: list[tuple[str, str]], text: str
    ) -> StopLedgerWrite: ...
