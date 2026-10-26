"""Typed input and output of the stuck-owner actor (OMN-17427).

Frozen dataclasses, not pydantic: the throughput tick and queue-status call the handler from the offline suites, where
only the standard library is installed.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# The controller is acting on an item only while its newest decision for it is a live lease or a dispatch this recent.
ACTING_WINDOW = timedelta(minutes=30)
# A red base, a stuck plugin version bump or a controller-owned item whose owner is not acting this long gets a lane.
STUCK_AFTER = timedelta(hours=2)
# A worker result of one of these kinds (kind, reason or blocker_kind) is an attempt that never ran.
NEVER_RAN = ("lab_capacity_unavailable", "host_tools_refused")
STATE_FILE = "stuck-owner-actor.json"
LANE_PREFIX = "cause-stuck-"

# (lane, repo, causes as <repo>:<check>, brief text) -> (dispatched, the runner's last line)
Dispatch = Callable[[str, str, list[str], str], tuple[bool, str]]
Finding = tuple[str, str, str, str]


@dataclass(frozen=True)
class ModelOwnerActingRequest:
    landing: Path  # the landing controller's state directory (decisions.jsonl, state.json, workers/)
    subject: str  # a cause key (cause:<owner>/<repo>:<signature>) or a PR (<owner>/<repo>#n or repo#n)
    now: datetime
    lease_id: int | None = (
        None  # the lease the reader named, when the subject has no decision row
    )
    window: timedelta = ACTING_WINDOW


@dataclass(frozen=True)
class ModelOwnerActingResult:
    acting: bool
    evidence: str  # the decision row (or its absence) the verdict read, quoted short


@dataclass(frozen=True)
class ModelStuckOwnerRequest:
    lines: list[str]  # the tick's lines so far
    watcher: Mapping[str, Any] | None  # the PR watcher state's raw dict
    landing: Path
    ledger: Path | None
    now: datetime
    state_dir: (
        Path  # where the actor keeps red_since and the dispatch record (STATE_FILE)
    )
    dispatch: (
        Dispatch | None
    )  # None: a read-only tick; it says what it would dispatch and records nothing
    parent: str | None = None
    stuck_after: timedelta = STUCK_AFTER
    window: timedelta = ACTING_WINDOW
    process_fix: list[Finding] = field(
        default_factory=list
    )  # the tick's process-fix findings, through the driver gate
    # (repo, base) -> the check names a pull_request_target workflow on the base produces; None when unread
    target_checks: Callable[[str, str], set[str] | None] | None = None
    # the landing scope's repositories; None when the scope is unread: then no base red or bump gets a lane
    scope: frozenset[str] | None = None


@dataclass(frozen=True)
class ModelStuckOwnerResult:
    lines: list[
        str
    ]  # the input lines, each owned-but-not-acting one rewritten as MISSING
    promoted: list[str] = field(
        default_factory=list
    )  # loops a rewrite turned from NOTE into MISSING
    found: list[Finding] = field(
        default_factory=list
    )  # the actor's own findings, one per stuck item


@dataclass(frozen=True)
class ModelDriverGateRequest:
    found: list[Finding]  # the tick's process-fix findings
    watcher: Mapping[str, Any] | None
    ledger: Path | None
    now: datetime
    # (repo, base) -> the check names a pull_request_target workflow on the base produces; None when unread
    target_checks: Callable[[str, str], set[str] | None] | None = None


@dataclass(frozen=True)
class ModelDriverGateResult:
    found: list[Finding]
