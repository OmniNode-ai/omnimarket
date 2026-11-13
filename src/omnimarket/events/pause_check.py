# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result of the pure pause-check findings compute node."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumFindingKind(StrEnum):
    """The kind of a pause-check finding, in the order the awk script prints them."""

    LABEL = "LABEL"
    ARM_OFF = "ARM-OFF"
    PARKED = "PARKED"
    HOLD = "HOLD"
    PR_ROW = "PR-ROW"
    REPO_PAUSE = "REPO-PAUSE"
    ORDER = "ORDER"
    CONTEXT = "CONTEXT"


class EnumPauseVerdict(StrEnum):
    """The verdict of one pull request's pause check."""

    CLEAR = "CLEAR"
    PAUSED = "PAUSED"


class ModelPauseCheckOverlay(BaseModel):
    """The deployment facts of the node; no key has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The awk cut width: the maximum number of bytes a snippet may carry (220 in the script).
    snippet_bytes: int = Field(gt=0)
    # The number of ORDER tokens printed at most (the 8 of `head -8`).
    max_order_tokens: int = Field(gt=0)
    # The environment variable naming the ledger file the findings are computed over.
    ledger_env: str = Field(min_length=1)
    # The environment variable naming the file the node's events are journalled to.
    journal_env: str = Field(min_length=1)


class ModelPauseCheckPr(BaseModel):
    """One pull request the pause check decides, with its labels and lane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The short repository name, without the owner prefix.
    repo: str = Field(min_length=1)
    # The pull request number.
    number: int = Field(gt=0)
    # The awk `me` lane, or None when the check runs without a lane.
    lane: str | None = None
    # The labels of the pull request, from the PR watcher state.
    labels: tuple[str, ...] = ()


class ModelPauseCheckRules(BaseModel):
    """The pause rules of the drain map the findings are decided against."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The row types that are reports and never pause a PR (drain_map REPORT_TYPES).
    report_types: tuple[str, ...]
    # The landing order printed below the ORDER findings (drain_map LANDING_ORDER).
    landing_order: str
    # The parked repositories and their reason (drain_map PARKED_REPOS).
    parked: dict[str, str]
    # The declared REPO-PAUSE ledger line numbers, by repository (drain_map repo_pause_lines).
    repo_pause_lines: dict[str, tuple[int, ...]]


class ModelPauseCheckRequest(BaseModel):
    """The pull requests, the pause rules, the ledger text and the time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prs: tuple[ModelPauseCheckPr, ...] = Field(min_length=1)
    rules: ModelPauseCheckRules
    # The whole ledger file decoded as utf-8 with errors=replace; lines split on "\n", 1-based.
    ledger_text: str
    # The UTC stamp an until= is compared against, like 2026-10-16T22:00:00Z.
    now: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    # True reads every row without the line prefilter (tests only).
    full_scan: bool


class ModelFinding(BaseModel):
    """One finding the awk script prints, with its exact rendered line."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumFindingKind
    # The ledger line number; None for LABEL, ARM-OFF and PARKED findings.
    line: int | None = None
    # The part of the line after "<KIND> "; for line findings it is "<line>: <snippet>".
    text: str = Field(min_length=1)

    def render(self) -> str:
        """The exact line the awk script prints for this finding."""
        if self.kind is EnumFindingKind.LABEL:
            return f"LABEL {self.text}"
        if self.kind is EnumFindingKind.ARM_OFF:
            return (
                f"ARM-OFF {self.text} (the auto-merge workflow arm gate: never arm; "
                "merge on the exact head, step 7)"
            )
        if self.kind is EnumFindingKind.PARKED:
            return f"PARKED {self.text}"
        return f"{self.kind.value} {self.text}"


_STOP_KINDS: frozenset[EnumFindingKind] = frozenset(
    {
        EnumFindingKind.LABEL,
        EnumFindingKind.PARKED,
        EnumFindingKind.HOLD,
        EnumFindingKind.PR_ROW,
        EnumFindingKind.REPO_PAUSE,
    }
)

_BEFORE_HOLD: frozenset[EnumFindingKind] = frozenset(
    {EnumFindingKind.LABEL, EnumFindingKind.ARM_OFF, EnumFindingKind.PARKED}
)


class ModelPauseCheckPrResult(BaseModel):
    """The findings, context rows, order tokens and verdict of one pull request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    number: int = Field(gt=0)
    # In awk print order: LABEL, ARM-OFF, PARKED, HOLD, PR-ROW, REPO-PAUSE, ORDER, each group
    # sorted by line number.
    findings: tuple[ModelFinding, ...] = ()
    # The CONTEXT rows, sorted by line number.
    context: tuple[ModelFinding, ...] = ()
    # "ORDER is not a stop. Landing order: <order>" when there is at least one ORDER finding,
    # else None.
    landing_order_note: str | None = None
    # The number of findings that count as stops.
    stops: int = Field(ge=0)
    verdict: EnumPauseVerdict
    # The PR references found in the ORDER finding lines, excluding this PR's own reference,
    # sorted unique, at most max_order_tokens of them.
    order_tokens: tuple[str, ...] = ()

    def with_holds(self, holds: tuple[ModelFinding, ...]) -> ModelPauseCheckPrResult:
        """The same result with its HOLD findings replaced by ``holds`` (the bus-fed answer), stops and verdict recomputed."""
        kept = tuple(f for f in self.findings if f.kind is not EnumFindingKind.HOLD)
        before = tuple(f for f in kept if f.kind in _BEFORE_HOLD)
        after = tuple(f for f in kept if f.kind not in _BEFORE_HOLD)
        stops = self.stops - (len(self.findings) - len(kept)) + len(holds)
        return self.model_copy(
            update={
                "findings": before + holds + after,
                "stops": stops,
                "verdict": EnumPauseVerdict.PAUSED
                if stops > 0
                else EnumPauseVerdict.CLEAR,
            }
        )

    def stop_lines(self) -> tuple[str, ...]:
        """The rendered findings that count as stops, in order."""
        return tuple(f.render() for f in self.findings if f.kind in _STOP_KINDS)

    def render_lines(self) -> tuple[str, ...]:
        """All awk stdout lines in order: findings, landing order note, context, verdict."""
        lines: list[str] = [f.render() for f in self.findings]
        if self.landing_order_note is not None:
            lines.append(self.landing_order_note)
        lines.extend(f.render() for f in self.context)
        if self.verdict is EnumPauseVerdict.PAUSED:
            lines.append(
                f"VERDICT: PAUSED, {self.stops} finding(s). "
                "Stop, and report every line above; do not flip, arm or merge."
            )
        else:
            lines.append(
                f"VERDICT: CLEAR, nothing pauses {self.repo}#{self.number}. "
                f"Read the {len(self.context)} CONTEXT row(s) above before acting."
            )
        return tuple(lines)


class ModelPauseCheckResult(BaseModel):
    """The per-pull-request results, in request order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    results: tuple[ModelPauseCheckPrResult, ...]


class EnumPauseHoldSource(StrEnum):
    """Where a pull request's HOLD findings came from: the bus-fed node, the counted file fallback, or nowhere."""

    NODE = "node"
    FILE_FALLBACK = "file-fallback"
    NONE = "none"


class ModelPauseCheckEvent(BaseModel):
    """The golden (``answered``) or error (``refused``) event of one pull request's pause check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    pr: str
    verdict: str
    source: EnumPauseHoldSource
    reason: str
    stops: int = Field(ge=0)


class ModelPauseDecision(BaseModel):
    """One pull request's decision: its result (None when UNDECIDED), where its HOLD findings came from, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str
    result: ModelPauseCheckPrResult | None
    source: EnumPauseHoldSource
    reason: str

    @property
    def verdict_word(self) -> str:
        """CLEAR, PAUSED, or UNDECIDED (a stop, never CLEAR)."""
        return "UNDECIDED" if self.result is None else self.result.verdict.value

    def as_json(self) -> dict[str, object]:
        """The entry of this pull request in the json output."""
        result = self.result
        return {
            "pr": self.pr,
            "verdict": self.verdict_word,
            "source": self.source.value,
            "reason": self.reason,
            "lines": [] if result is None else list(result.render_lines()),
            "stop_lines": [] if result is None else list(result.stop_lines()),
            "order_tokens": [] if result is None else list(result.order_tokens),
        }
