# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the handoff check: request, result, policy, ledger rows and facts."""

from __future__ import annotations

import datetime as dt
import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def parse_ts(value: str) -> dt.datetime:
    return dt.datetime.strptime(value.strip(), TS_FMT).replace(tzinfo=dt.UTC)


def fmt_ts(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).strftime(TS_FMT)


class EnumHandoffCommand(StrEnum):
    ASSEMBLE = "assemble"
    RESUME = "resume"
    RECORD_SLICE = "record-slice"


class ModelHandoffCheckRequest(BaseModel):
    """One handoff check call; the paths are read on the host that runs the node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: EnumHandoffCommand
    ledger: str | None = None
    archive_dir: str | None = None
    now: str | None = Field(
        default=None, description="UTC time to evaluate at; the clock when absent"
    )
    pr_reads: str | None = None
    probes: str | None = None
    # assemble
    out_dir: str | None = None
    session: str | None = None
    lane: str | None = None
    goal_file: str | None = None
    statuses: str | None = None
    unverified_file: str | None = None
    limits_file: str | None = None
    plans: tuple[str, ...] = ()
    prs: tuple[str, ...] = ()
    readers: bool = False
    # resume
    kb_clone: str | None = None
    ref: str = "origin/main"
    directory: str | None = None
    # record-slice
    since: str | None = None
    until: str | None = None
    out: str | None = None


class ModelDecisionRef(BaseModel):
    """One ruling or consent placed in a dropped-decision bucket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stamp: str
    row_type: str
    ticket: str
    evidence: str


class ModelDroppedDecisions(BaseModel):
    """Rulings and consents with no lane, PR or finish inside the horizon after them."""

    model_config = ConfigDict(extra="forbid")

    dropped: list[ModelDecisionRef] = Field(default_factory=list)
    evidenced: list[ModelDecisionRef] = Field(default_factory=list)
    unlinkable: list[ModelDecisionRef] = Field(default_factory=list)
    pending_no_evidence_yet: list[ModelDecisionRef] = Field(default_factory=list)
    pending_evidenced: list[ModelDecisionRef] = Field(default_factory=list)

    @property
    def judged(self) -> int:
        return len(self.dropped) + len(self.evidenced)

    def summary_line(self) -> str:
        return (
            f"DROPPED-DECISIONS dropped={len(self.dropped)} judged={self.judged} "
            f"unlinkable={len(self.unlinkable)} "
            f"pending_no_evidence_yet={len(self.pending_no_evidence_yet)} "
            f"pending_evidenced={len(self.pending_evidenced)}"
        )


class ModelHandoffCheckResult(BaseModel):
    """What the check printed and how it ended: exit 0 ok, 1 refused, 2 bad input, 3 none."""

    model_config = ConfigDict(extra="forbid")

    command: EnumHandoffCommand
    exit_code: int
    output: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    refused: list[str] = Field(default_factory=list)
    dropped: ModelDroppedDecisions | None = None
    stale_lines: int = 0
    written_path: str | None = None


# --- policy (the handoff_check section of contract.yaml) ----------------------------------


class ModelOutputPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    directory: str
    filename: str


class ModelRulingGatePolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    row_types: tuple[str, ...]
    statuses: tuple[str, ...]
    window_hours: int
    umbrella_min_lanes: int
    umbrella_scan_days: int
    done_outcomes: tuple[str, ...]


class ModelDroppedDecisionPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    hours: int
    scan_hours: int
    evidence_row_types: tuple[str, ...]
    evidence_pr_cells: tuple[str, ...]


class ModelLanesPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_scan_days: int
    max_lines_per_class: int
    live_within_minutes: int
    dead_after_hours: int


class ModelHoldsPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_lines: int


class ModelStoppedWorkPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    window_hours: int
    ok_outcomes: tuple[str, ...]
    max_lines: int


class ModelOperatorAsksPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    window_hours: int
    max_lines: int


class ModelPrsPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    window_hours: int
    max_prs: int
    reader: str


class ModelLimitsPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    window_hours: int


class ModelStalePolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    default_seconds: int
    by_source: dict[str, int] = Field(default_factory=dict)

    def ttl_for(self, kind: str) -> int:
        return self.by_source.get(kind, self.default_seconds)


class ModelProbeSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    kind: str
    env: str
    max_age_seconds: int = 0


class ModelSectionSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    title: str
    source: str


class ModelHandoffPolicy(BaseModel):
    """The declared handoff contract: sections, statuses, stale ages and probes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: int
    output: ModelOutputPolicy
    ruling_gate: ModelRulingGatePolicy
    dropped_decision: ModelDroppedDecisionPolicy
    lanes: ModelLanesPolicy
    holds: ModelHoldsPolicy
    stopped_work: ModelStoppedWorkPolicy
    operator_asks: ModelOperatorAsksPolicy
    prs: ModelPrsPolicy
    limits: ModelLimitsPolicy
    stale: ModelStalePolicy
    probes: tuple[ModelProbeSpec, ...]
    sections: tuple[ModelSectionSpec, ...]


# --- ledger rows, facts, probes -----------------------------------------------------------


class ModelLedgerRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: dt.datetime
    type: str
    cells: dict[str, str]
    raw: str
    ref: str

    @property
    def tickets(self) -> set[str]:
        return {
            self.cells[k] for k in ("ticket", "mechanism-ticket") if self.cells.get(k)
        }

    @property
    def text(self) -> str:
        return self.raw.rsplit(" | ", 1)[-1]

    @property
    def body(self) -> str:
        """The row without its own leading timestamp, for finding a citation of another row's."""
        return self.raw[20:]


_FACT_RE = re.compile(r"^- (?P<text>.*) \[(?P<meta>src: .*)\]$")


class ModelFact(BaseModel):
    """One factual line: its source, the time it was read, and the age after which it is stale."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    source: str
    read_at: str
    ttl: int = 0
    probe: str = ""
    val: str = ""

    def stale(self, now: dt.datetime) -> bool:
        return (
            self.ttl > 0 and (now - parse_ts(self.read_at)).total_seconds() > self.ttl
        )

    def render(self, now: dt.datetime) -> str:
        def clean(value: str) -> str:
            return re.sub(r"[;\]\n]", ",", value)

        parts = [
            f"src: {clean(self.source)}",
            f"read: {self.read_at}",
            f"ttl: {self.ttl}",
        ]
        if self.probe:
            parts.append(f"probe: {clean(self.probe)}")
        if self.val:
            parts.append(f"val: {clean(self.val)}")
        if self.stale(now):
            parts.append("STALE")
        return f"- {self.text.replace(chr(10), ' ')} [{'; '.join(parts)}]"

    @classmethod
    def parse_line(cls, line: str) -> ModelFact | None:
        match = _FACT_RE.match(line.rstrip())
        if not match:
            return None
        meta: dict[str, str] = {}
        for part in match.group("meta").split("; "):
            key, _, value = part.partition(": ")
            meta[key] = value
        if "read" not in meta or "src" not in meta:
            return None
        return cls(
            text=match.group("text"),
            source=meta["src"],
            read_at=meta["read"],
            ttl=int(meta.get("ttl", "0") or 0),
            probe=meta.get("probe", ""),
            val=meta.get("val", ""),
        )


class ModelProbeRecord(BaseModel):
    """One service probe read: healthy, unhealthy (False) or unconfigured (None)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    healthy: bool | None
    detail: str
    read_at: str
    probe: str

    @property
    def verdict(self) -> str:
        if self.healthy is None:
            return "unconfigured"
        return "healthy" if self.healthy else "UNHEALTHY"


class ModelAccount(BaseModel):
    """The status of one ruling or consent in the window, or why it has none."""

    model_config = ConfigDict(extra="forbid")

    item: str
    row: ModelLedgerRow
    status: str = ""
    evidence: str = ""
    how: str = ""
    problem: str = ""


class ModelAssembledHandoff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    refused: list[str]
    dropped: ModelDroppedDecisions
    stale_lines: int
    filename: str
