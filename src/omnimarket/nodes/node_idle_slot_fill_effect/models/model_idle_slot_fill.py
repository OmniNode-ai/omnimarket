# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the idle-slot fill and the idle-turn guard (OMN-17427).

Frozen dataclasses, not pydantic: the request carries callables and the tick passes plain mappings through it.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Preserve the existing idle-fill import paths for callers of the shared crossing.
from omnimarket.models.model_backlog_item import (
    POLICY_CONTEXT_KEY as POLICY_CONTEXT_KEY,
)
from omnimarket.models.model_backlog_item import (
    REPO_KINDS_CONTEXT_KEY as REPO_KINDS_CONTEXT_KEY,
)
from omnimarket.models.model_backlog_item import (
    ModelBacklogItem as ModelBacklogItem,
)
from omnimarket.models.model_backlog_item import (
    backlog_item_refusal as backlog_item_refusal,
)


@dataclass(frozen=True)
class ModelBacklogSources:
    """Live state once the file backlog is spent (OMN-17427). ledger_tickets answers (tickets a live CLAIM names,
    ticket -> TERMINAL triage verdict blocked on a missing readback), raising when the ledger cannot be read.
    experiments answers the experiment designs (the idle_slot_fill overlay), raising when none is configured.
    ticket_rule delegates ticket admission to omnimarket. All None generates nothing."""

    linear_snapshot: Path | None = None
    m4_carriers: Path | None = None
    scope: Path | None = None
    experiments: Callable[[], list[dict[str, Any]]] | None = None
    ledger_tickets: Callable[[], tuple[set[str], dict[str, str]]] | None = None
    ticket_rule: Callable[[dict[str, Any]], dict[str, Any]] | None = None


@dataclass(frozen=True)
class ModelIdleSlotFillRequest:
    """What the throughput tick read: the live pool hosts, its own finding lines, the lanes the merge sweep will
    place this tick, the standing pillar backlog, the keys it already sent inside the window, and the repositories
    the merge sweep's one scope setting rules out (``scope_unread`` names why that setting could not be read). ``placing``
    counts, by engine (claude, codex, glm), the lanes the fill sent that no host holds yet: each reserves a free slot.
    ``fixer_holds`` carries active repository HOLD rows read by the effect; an
    unread HOLD ledger refuses repository work with ``fixer_holds_unread``."""

    now: str
    hosts: list[dict[str, Any]]
    lines: list[str]
    sweep_planned: int = 0
    backlog: list[dict[str, Any]] = field(default_factory=list)
    dispatched: dict[str, dict[str, Any]] = field(default_factory=dict)
    window_hours: float = 3.0
    parent: str | None = None
    generated: list[dict[str, Any]] = field(default_factory=list)
    out_of_scope: Mapping[str, str] = field(default_factory=dict)
    scope_unread: str = ""
    placing: Mapping[str, int] = field(default_factory=dict)
    queue_reserved: Mapping[str, int] = field(
        default_factory=dict
    )  # starts and unblocked waits in the dispatch queue
    standing_tickets: Mapping[str, str] = field(
        default_factory=dict
    )  # declared child ticket for each backlog class
    watcher_state: Mapping[str, Any] | None = None
    completed: Mapping[str, str] = field(
        default_factory=dict
    )  # experiment key -> "<lane> at <ended_at>"
    fixer_holds: Mapping[str, str] = field(default_factory=dict)
    fixer_holds_unread: str = ""
    # Kinds whose items must name a repository (ModelBacklogItem), read by the caller from the
    # idle_slot_fill overlay; none by default.
    repo_required_kinds: Collection[str] = ()


@dataclass(frozen=True)
class ModelIdleSlotFillResult:
    claude_free: int = 0
    codex_only_free: int = 0
    sends: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    idle_after: int = 0
    glm_only_free: int = 0
    dropped: list[dict[str, str]] = field(default_factory=list)
    # Items that failed ModelBacklogItem (a prose engine, a missing repository): each is also in
    # ``skipped`` with its reason, and none is dispatched (OMN-21475).
    backlog_mismatched: int = 0


@dataclass(frozen=True)
class ModelIdleTurnGuardRequest:
    """One Stop turn of the orchestrator session: the tool calls it made ({id, name, input}) and every tool result it
    read ({id, text}), in order; a result's id is the tool call's that produced it."""

    tool_uses: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]


@dataclass(frozen=True)
class ModelIdleTurnGuardResult:
    refuse: bool = False
    reason: str = ""
    shown: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ModelLabFillRequest:
    """The throughput tick's call: the facts it read (``request`` holds ``lab_headroom``), its finding lines, its
    `sweep:` line, the lab fill's state directory (``lab_fill_state_dir()``, never the tick's own), the backlog file
    (None: pillar-backlog.json there), the remote-lane runner,
    the parent lane, and whether the tick is read-only (--no-replace). ``record`` appends the tick's row for one lane sent
    (action, subject, detail) and answers the reason it could not, else None (OMN-19854). ``out_of_scope`` is the merge
    sweep's scope setting ({repo: ruling}) the tick read, or the reason it could not read it (OMN-17427).
    sources=None reads handler.default_sources(now, os.environ) (OMN-17427)."""

    now: str
    request: dict[str, Any]
    lines: list[str]
    launch: str | None
    state_dir: Path
    backlog: str | None
    runner: Path
    parent: str | None = None
    read_only: bool = False
    window_hours: float = 3.0
    lanes_dir: Path | None = (
        None  # the remote-lane runner's receipt directory, read for the fill's lanes still placing (None: the runner's default)
    )
    record: Callable[[str, str, str], str | None] | None = None
    sources: ModelBacklogSources | None = None
    out_of_scope: Mapping[str, str] | str | None = None
    queue_reserved: Mapping[str, int] = field(
        default_factory=dict
    )  # reserved against this tick's lab_headroom snapshot


@dataclass(frozen=True)
class ModelLabFillResult:
    found: list[tuple[str, str, str, str]] = field(default_factory=list)
