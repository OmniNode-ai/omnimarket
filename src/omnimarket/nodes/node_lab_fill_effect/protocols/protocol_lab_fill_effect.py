# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ports the lab-fill effect handlers act through (OMN-20668).

Every handler takes its ports in its constructor, so the golden and error chains
run against fakes and the deployment wires the local adapters
(``local_lab_fill_adapters``) or its own. A port that cannot answer raises
``LabFillPortError``; the handler turns it into a typed result, never a silence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from omnimarket.models.work_ledger_append import (
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)


class LabFillPortError(RuntimeError):
    """A port could not read or act; the message names what failed."""


@dataclass(frozen=True)
class LabFillCommandOutcome:
    """The exit status and output of one command a port ran."""

    returncode: int
    stdout: str
    stderr: str


class ProtocolLabFillClock(Protocol):
    def now_epoch_s(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class ProtocolLabFillPlacementReader(Protocol):
    """The remote-lane runner's own pool read and its limited-host markers."""

    def read_pool(self) -> Sequence[Mapping[str, object]]: ...

    def limited_hosts(self) -> frozenset[str]: ...


class ProtocolLabFillLedgerReader(Protocol):
    """Reads the rolling ledger for this fire's STATUS row."""

    def status_row_time(self, ledger_path: str, run_key: str) -> str | None: ...


class ProtocolLabFillApprovedWork(Protocol):
    """The declared approved-work list: its depth, and one row by id."""

    def depth(self, path: str) -> int | None: ...

    def row(self, path: str, row_id: str) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class LabFillClaimFacts:
    """The ledger's claim store as read: index records by ticket and the open CLAIM replay.

    Either is the text of why it could not be read; the two fail separately.
    """

    index: Mapping[str, Mapping[str, str]] | str
    open_claims: Sequence[Mapping[str, str]] | str
    staleness_hours: float


class ProtocolLabFillOwnerReader(Protocol):
    """The ownership sources: the claim store, the PR claim registry and the PR watcher."""

    def claims(self, ledger_path: str, tickets: Sequence[str]) -> LabFillClaimFacts: ...

    def pr_claims(self, cli_path: str) -> Mapping[str, str]: ...

    def watcher_merged(self, state_path: str) -> Mapping[str, Sequence[str]]: ...


class ProtocolLabFillLiveChecks(Protocol):
    """The checks made for one lane right before its launch; an empty answer clears it."""

    def skip_reason(
        self,
        *,
        ticket: str,
        kind: str,
        pr: str,
        operator_id: str,
        ledger_path: str,
    ) -> tuple[str, str]: ...


class ProtocolLabFillBriefBlocks(Protocol):
    """The committed standing rules and the delegation and lab blocks of a brief."""

    def blocks(self, *, ticket: str, brief_path: str) -> str: ...


class ProtocolLabFillLaneLauncher(Protocol):
    """Runs the remote-lane runner script with the given arguments (``run ... --detach``)."""

    def run(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout_s: float,
    ) -> LabFillCommandOutcome: ...


class ProtocolLabFillReceiptReader(Protocol):
    """Reads one runner receipt file; None when it is absent or unreadable."""

    def read(self, path: str) -> Mapping[str, object] | None: ...


class ProtocolLabFillStatusAppender(Protocol):
    """Appends exact ledger rows through the ledger host's bus append."""

    async def append(
        self, request: ModelWorkLedgerAppendRequest, *, timeout_s: float
    ) -> ModelWorkLedgerAppendReceipt | None: ...


class ProtocolLabFillResultWriter(Protocol):
    """Writes the fire's result file for the launchd driver and SessionStart."""

    def write(self, path: str, value: Mapping[str, object]) -> None: ...
