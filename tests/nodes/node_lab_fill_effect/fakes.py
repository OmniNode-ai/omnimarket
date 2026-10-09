# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fake ports for the lab-fill effect chains (OMN-20668): the handlers run unchanged, the world is scripted."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_lab_fill_effect.protocols import (
    LabFillClaimFacts,
    LabFillCommandOutcome,
    LabFillPortError,
)

START = 1_790_000_000.0  # 2026-09-21T14:13:20Z


class FakeClock:
    def __init__(self, start: float = START, step: float = 0.0) -> None:
        self.t = start
        self.step = step
        self.sleeps: list[float] = []

    def now_epoch_s(self) -> float:
        self.t += self.step
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


@dataclass
class FakePlacement:
    readings: Sequence[Mapping[str, object]] = ()
    limited: frozenset[str] = frozenset()
    fail: str = ""

    def read_pool(self) -> Sequence[Mapping[str, object]]:
        if self.fail:
            raise LabFillPortError(self.fail)
        return self.readings

    def limited_hosts(self) -> frozenset[str]:
        if self.fail:
            raise LabFillPortError(self.fail)
        return self.limited


@dataclass
class FakeLedgerReader:
    delivered: str | None = None
    fail: str = ""

    def status_row_time(self, ledger_path: str, run_key: str) -> str | None:
        if self.fail:
            raise LabFillPortError(self.fail)
        return self.delivered


@dataclass
class FakeApproved:
    rows: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    list_depth: int | None = None
    fail: str = ""

    def depth(self, path: str) -> int | None:
        return self.list_depth

    def row(self, path: str, row_id: str) -> Mapping[str, object]:
        if self.fail or row_id not in self.rows:
            raise LabFillPortError(
                self.fail or f"APPROVED-ROW {row_id} missing or duplicate row"
            )
        return self.rows[row_id]


@dataclass
class FakeChecks:
    answer: tuple[str, str] = ("", "")
    fail: str = ""
    calls: list[dict[str, str]] = field(default_factory=list)

    def skip_reason(
        self, *, ticket: str, kind: str, pr: str, operator_id: str, ledger_path: str
    ) -> tuple[str, str]:
        self.calls.append(
            {"ticket": ticket, "kind": kind, "pr": pr, "operator_id": operator_id}
        )
        if self.fail:
            raise LabFillPortError(self.fail)
        return self.answer


@dataclass
class FakeBlocks:
    text: str = "STANDING RULES\n\n3a.9 Delegation\n\n3a.10 lab\n"
    fail: str = ""

    def blocks(self, *, ticket: str, brief_path: str) -> str:
        if self.fail:
            raise LabFillPortError(self.fail)
        return self.text


@dataclass
class FakeRunner:
    outcome: LabFillCommandOutcome = field(
        default_factory=lambda: LabFillCommandOutcome(
            0, "DETACHED lane=x receipt=/state/x/receipt.json\n", ""
        )
    )
    fail: str = ""
    calls: list[tuple[list[str], dict[str, str], float]] = field(default_factory=list)

    def run(
        self, args: Sequence[str], *, env: Mapping[str, str], timeout_s: float
    ) -> LabFillCommandOutcome:
        self.calls.append((list(args), dict(env), timeout_s))
        if self.fail:
            raise LabFillPortError(self.fail)
        return self.outcome


@dataclass
class FakeReceipts:
    """Receipts by path; a list is consumed one read at a time, the last one repeating."""

    by_path: Mapping[str, Sequence[Mapping[str, object] | None]]
    reads: int = 0

    def read(self, path: str) -> Mapping[str, object] | None:
        self.reads += 1
        seq = self.by_path.get(path)
        if not seq:
            return None
        return seq.pop(0) if len(seq) > 1 else seq[0]


@dataclass
class FakeOwners:
    claims_facts: LabFillClaimFacts | None = None
    registry: Mapping[str, str] | None = None
    merged: Mapping[str, Sequence[str]] | None = None
    fail: Mapping[str, str] = field(default_factory=dict)
    called: list[str] = field(default_factory=list)

    def claims(self, ledger_path: str, tickets: Sequence[str]) -> LabFillClaimFacts:
        self.called.append("claims")
        if "claims" in self.fail:
            raise LabFillPortError(self.fail["claims"])
        assert self.claims_facts is not None
        return self.claims_facts

    def pr_claims(self, cli_path: str) -> Mapping[str, str]:
        self.called.append("pr_claims")
        if "pr_claims" in self.fail:
            raise LabFillPortError(self.fail["pr_claims"])
        return self.registry or {}

    def watcher_merged(self, state_path: str) -> Mapping[str, Sequence[str]]:
        self.called.append("watcher")
        if "watcher" in self.fail:
            raise LabFillPortError(self.fail["watcher"])
        return self.merged or {}


@dataclass
class FakeAppender:
    statuses: list[EnumWorkLedgerAppendStatus | None] = field(
        default_factory=lambda: [EnumWorkLedgerAppendStatus.ACCEPTED]
    )
    raises: Exception | None = None
    sent: list[ModelWorkLedgerAppendRequest] = field(default_factory=list)

    async def append(
        self, request: ModelWorkLedgerAppendRequest, *, timeout_s: float
    ) -> ModelWorkLedgerAppendReceipt | None:
        self.sent.append(request)
        if self.raises is not None:
            raise self.raises
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if status is None:
            return None
        return ModelWorkLedgerAppendReceipt(
            request_id=UUID(str(request.request_id)),
            status=status,
            exit_code=0 if status is not EnumWorkLedgerAppendStatus.ERROR else 1,
            message=status.value,
            ledger_lines=[41] if status is EnumWorkLedgerAppendStatus.ACCEPTED else [],
            ledger_host="ledger",
            duration_ms=3,
        )


@dataclass
class FakeWriter:
    written: dict[str, Mapping[str, object]] = field(default_factory=dict)
    fail: bool = False

    def write(self, path: str, value: Mapping[str, object]) -> None:
        if self.fail:
            raise LabFillPortError("disk full")
        self.written[path] = value
