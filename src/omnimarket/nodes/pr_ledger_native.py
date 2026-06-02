# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Durable, reconstructable PR ledger — derived projection (OMN-12569).

Truth semantics (non-negotiable)
--------------------------------
The PR ledger is a *derived projection* materialized from typed source events:
GitHub events, workflow runs, merge-group state, and orchestrator actions. It is
**not authoritative truth by itself** — authoritative truth remains GitHub state
plus durable orchestrator receipts. Every ledger entry therefore carries:

  * ``provenance``  — the ordered source events it was folded from, each with a
    workflow run, merge-group SHA, branch SHA, orchestrator action, and
    timestamp (DT-005 provenance declaration);
  * ``provenance_kind = "derived_projection"`` on the ledger itself.

Because the ledger is derived, it must be reconstructable from its source
events. ``reconstruct_pr_ledger`` replays an unordered event log
deterministically and produces a projection identical to the one built
incrementally by the live sweep path. If reconstruction did not converge, the
ledger would degrade to merely "persistent" — another manually-trusted state
store — which the doctrine prohibits.

Persistence
-----------
The projection lands in a control-plane durable store, not a repo artifact:
``ProjectionDatabasePrLedgerStore`` UPSERTs entries through the existing
``ProtocolProjectionDatabaseSync`` boundary (Postgres in production; the
in-memory adapter in tests). ``InMemoryPrLedgerStore`` is the deterministic
local/test surface.

Related:
    - OMN-12569: Orchestrator owns a durable, reconstructable PR ledger.
    - OMN-12504: Merge queue recovery epic.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.projection.protocol_database import ProtocolProjectionDatabaseSync

# DT-003 freshness SLA: the projection is advisory and must be refreshed each
# sweep. Consumers treat entries older than this window as stale; the value is
# declared here (not hardcoded at call sites) so the SLA is discoverable.
PR_LEDGER_FRESHNESS_SLA_SECONDS = 900
PROVENANCE_KIND = "derived_projection"


# ---------------------------------------------------------------------------
# Enums — closed vocabularies for source-event kinds, orchestrator actions,
# and final conclusions.
# ---------------------------------------------------------------------------


class EnumPrLedgerEventKind(StrEnum):
    """Kind of source event folded into the projection."""

    PR_INVENTORIED = "pr_inventoried"
    WORKFLOW_RUN_OBSERVED = "workflow_run_observed"
    MERGE_GROUP_SHA_MINTED = "merge_group_sha_minted"
    RERUN_ATTEMPTED = "rerun_attempted"
    FINAL_CONCLUSION = "final_conclusion"


class EnumOrchestratorAction(StrEnum):
    """Orchestrator action that produced a source event (provenance)."""

    INVENTORY = "inventory"
    OBSERVE = "observe"
    ENQUEUE = "enqueue"
    REQUEUE = "requeue"
    MERGE = "merge"
    FIX = "fix"
    SKIP = "skip"


class EnumPrLedgerConclusion(StrEnum):
    """Terminal conclusion for a PR within a sweep run."""

    PENDING = "pending"
    MERGED = "merged"
    FAILED = "failed"
    SKIPPED = "skipped"


# ---------------------------------------------------------------------------
# Source event — the replayable unit of truth derivation.
# ---------------------------------------------------------------------------


class ModelPrLedgerSourceEvent(BaseModel):
    """One observed source event for a PR within a sweep run.

    Source events are the authoritative inputs the projection is derived from.
    The ledger never invents a field that is not present on some source event;
    reconstruction replays exactly these events.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumPrLedgerEventKind = Field(..., description="Source event kind.")
    run_id: str = Field(
        ...,
        min_length=1,
        description="Human-readable sweep run identifier (ledger partition key).",
    )
    correlation_id: UUID = Field(..., description="Sweep correlation id.")
    repo: str = Field(..., description="Repo slug, e.g. 'OmniNode-ai/omnimarket'.")
    pr_number: int = Field(..., ge=1, description="GitHub PR number.")
    head_sha: str | None = Field(
        default=None, description="Branch (head) SHA at observation time."
    )
    workflow_run_id: int | None = Field(
        default=None, description="GitHub Actions workflow run id, when applicable."
    )
    merge_group_sha: str | None = Field(
        default=None, description="Synthetic merge-group SHA, when applicable."
    )
    conclusion: EnumPrLedgerConclusion | None = Field(
        default=None, description="Terminal conclusion (FINAL_CONCLUSION events)."
    )
    orchestrator_action: EnumOrchestratorAction = Field(
        ..., description="Orchestrator action that produced this event."
    )
    observed_at: str = Field(
        ...,
        min_length=1,
        description="ISO-8601 timestamp when the event was observed.",
    )


# ---------------------------------------------------------------------------
# Provenance + entry + ledger projection models.
# ---------------------------------------------------------------------------


class ModelPrLedgerProvenance(BaseModel):
    """Per-entry provenance record (one per folded source event).

    Records workflow run, merge-group SHA, branch SHA, orchestrator action, and
    timestamp — the full evidence trail required by OMN-12569.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_kind: EnumPrLedgerEventKind = Field(...)
    workflow_run: int | None = Field(default=None)
    merge_group_sha: str | None = Field(default=None)
    branch_sha: str | None = Field(default=None)
    orchestrator_action: EnumOrchestratorAction = Field(...)
    observed_at: str = Field(...)


class ModelPrLedgerEntry(BaseModel):
    """Materialized ledger entry for one PR within a sweep run.

    Derived by folding the PR's ordered source events. Carries the listed
    OMN-12569 fields (run id, head SHA, merge-group SHAs, rerun attempts, final
    conclusion) plus the full provenance trail.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(...)
    correlation_id: UUID = Field(...)
    repo: str = Field(...)
    pr_number: int = Field(..., ge=1)
    head_sha: str | None = Field(default=None)
    workflow_run_ids: tuple[int, ...] = Field(default_factory=tuple)
    merge_group_shas: tuple[str, ...] = Field(default_factory=tuple)
    rerun_attempts: int = Field(default=0, ge=0)
    conclusion: EnumPrLedgerConclusion = Field(default=EnumPrLedgerConclusion.PENDING)
    last_action: EnumOrchestratorAction | None = Field(default=None)
    first_observed_at: str | None = Field(default=None)
    last_observed_at: str | None = Field(default=None)
    provenance: tuple[ModelPrLedgerProvenance, ...] = Field(default_factory=tuple)


class ModelPrLedger(BaseModel):
    """The durable PR ledger projection for one sweep run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(...)
    provenance_kind: str = Field(default=PROVENANCE_KIND)
    freshness_sla_seconds: int = Field(default=PR_LEDGER_FRESHNESS_SLA_SECONDS, ge=0)
    last_event_at: str | None = Field(default=None)
    entries: tuple[ModelPrLedgerEntry, ...] = Field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Derivation — fold a single source event into an entry.
# ---------------------------------------------------------------------------


def _provenance_of(event: ModelPrLedgerSourceEvent) -> ModelPrLedgerProvenance:
    return ModelPrLedgerProvenance(
        event_kind=event.kind,
        workflow_run=event.workflow_run_id,
        merge_group_sha=event.merge_group_sha,
        branch_sha=event.head_sha,
        orchestrator_action=event.orchestrator_action,
        observed_at=event.observed_at,
    )


def fold_event(
    entry: ModelPrLedgerEntry | None,
    event: ModelPrLedgerSourceEvent,
) -> ModelPrLedgerEntry:
    """Fold one source event into a (possibly absent) entry → new entry.

    Pure and deterministic: no I/O, no clocks, no defaults pulled from the
    environment. The same (entry, event) always yields the same result, which is
    what makes the projection reconstructable.
    """
    if entry is None:
        base = ModelPrLedgerEntry(
            run_id=event.run_id,
            correlation_id=event.correlation_id,
            repo=event.repo,
            pr_number=event.pr_number,
            first_observed_at=event.observed_at,
        )
    else:
        base = entry

    workflow_run_ids = base.workflow_run_ids
    if (
        event.workflow_run_id is not None
        and event.workflow_run_id not in workflow_run_ids
    ):
        workflow_run_ids = (*workflow_run_ids, event.workflow_run_id)

    merge_group_shas = base.merge_group_shas
    if (
        event.merge_group_sha is not None
        and event.merge_group_sha not in merge_group_shas
    ):
        merge_group_shas = (*merge_group_shas, event.merge_group_sha)

    rerun_attempts = base.rerun_attempts
    if event.kind is EnumPrLedgerEventKind.RERUN_ATTEMPTED:
        rerun_attempts += 1

    conclusion = base.conclusion
    if (
        event.kind is EnumPrLedgerEventKind.FINAL_CONCLUSION
        and event.conclusion is not None
    ):
        conclusion = event.conclusion

    head_sha = event.head_sha if event.head_sha is not None else base.head_sha
    first_observed_at = base.first_observed_at or event.observed_at

    return base.model_copy(
        update={
            "head_sha": head_sha,
            "workflow_run_ids": workflow_run_ids,
            "merge_group_shas": merge_group_shas,
            "rerun_attempts": rerun_attempts,
            "conclusion": conclusion,
            "last_action": event.orchestrator_action,
            "first_observed_at": first_observed_at,
            "last_observed_at": event.observed_at,
            "provenance": (*base.provenance, _provenance_of(event)),
        }
    )


# ---------------------------------------------------------------------------
# Durable store boundary.
# ---------------------------------------------------------------------------


@runtime_checkable
class ProtocolPrLedgerStore(Protocol):
    """Control-plane durable boundary for the PR ledger projection."""

    def load(self, run_id: str) -> ModelPrLedger:
        """Return the current projection for a sweep run (empty if unseen)."""
        ...

    def apply(self, event: ModelPrLedgerSourceEvent) -> ModelPrLedgerEntry:
        """Fold one source event and persist the updated entry."""
        ...


class InMemoryPrLedgerStore:
    """Deterministic in-memory ledger store for local runs and tests."""

    def __init__(self) -> None:
        # run_id -> (repo, pr_number) -> entry
        self._runs: dict[str, dict[tuple[str, int], ModelPrLedgerEntry]] = {}
        self._last_event_at: dict[str, str] = {}

    def load(self, run_id: str) -> ModelPrLedger:
        entries = self._runs.get(run_id, {})
        return _build_ledger(
            run_id=run_id,
            entries=entries.values(),
            last_event_at=self._last_event_at.get(run_id),
        )

    def apply(self, event: ModelPrLedgerSourceEvent) -> ModelPrLedgerEntry:
        run = self._runs.setdefault(event.run_id, {})
        key = (event.repo, event.pr_number)
        entry = fold_event(run.get(key), event)
        run[key] = entry
        prev = self._last_event_at.get(event.run_id)
        if prev is None or event.observed_at > prev:
            self._last_event_at[event.run_id] = event.observed_at
        return entry


class ProjectionDatabasePrLedgerStore:
    """Ledger store backed by the control-plane projection database.

    UPSERTs each materialized entry through ``ProtocolProjectionDatabaseSync``
    (Postgres in production; the in-memory projection adapter in tests). Rows are
    keyed by ``(run_id, repo, pr_number)`` so re-applying an event is idempotent.
    """

    def __init__(
        self,
        database: ProtocolProjectionDatabaseSync,
        *,
        table: str,
    ) -> None:
        self._db = database
        self._table = table

    def load(self, run_id: str) -> ModelPrLedger:
        rows = self._db.query(self._table, {"run_id": run_id})
        entries = [_entry_from_row(row) for row in rows]
        last_event_at = max(
            (e.last_observed_at for e in entries if e.last_observed_at is not None),
            default=None,
        )
        return _build_ledger(
            run_id=run_id, entries=entries, last_event_at=last_event_at
        )

    def apply(self, event: ModelPrLedgerSourceEvent) -> ModelPrLedgerEntry:
        existing = self._load_entry(event.run_id, event.repo, event.pr_number)
        entry = fold_event(existing, event)
        self._db.upsert(
            self._table,
            "run_id,repo,pr_number",
            _row_from_entry(entry),
        )
        return entry

    def _load_entry(
        self, run_id: str, repo: str, pr_number: int
    ) -> ModelPrLedgerEntry | None:
        rows = self._db.query(
            self._table,
            {"run_id": run_id, "repo": repo, "pr_number": pr_number},
        )
        if not rows:
            return None
        return _entry_from_row(rows[0])


# ---------------------------------------------------------------------------
# Reducer entry point + reconstruction.
# ---------------------------------------------------------------------------


def apply_pr_ledger_event(
    event: ModelPrLedgerSourceEvent,
    *,
    store: ProtocolPrLedgerStore,
) -> ModelPrLedgerEntry:
    """Fold a source event into the durable ledger projection."""
    return store.apply(event)


def reconstruct_pr_ledger(
    events: tuple[ModelPrLedgerSourceEvent, ...],
) -> ModelPrLedger:
    """Rebuild the ledger from an arbitrary source-event log.

    Replays the events deterministically, independent of input order, and
    returns the materialized projection. Given the same source events (in any
    order), this yields the projection the live sweep path built incrementally —
    the reconstructability guarantee OMN-12569 requires.

    Events for distinct PRs fold independently; per-PR events are ordered by
    ``observed_at`` so a shuffled durable log still rebuilds identical state.
    """
    if not events:
        raise ValueError("reconstruct_pr_ledger requires at least one source event")

    run_ids = {event.run_id for event in events}
    if len(run_ids) != 1:
        raise ValueError(
            f"reconstruct_pr_ledger expects a single run_id, got {sorted(run_ids)}"
        )
    run_id = next(iter(run_ids))

    ordered = sorted(
        events, key=lambda e: (e.repo, e.pr_number, e.observed_at, e.kind.value)
    )
    entries: dict[tuple[str, int], ModelPrLedgerEntry] = {}
    last_event_at: str | None = None
    for event in ordered:
        key = (event.repo, event.pr_number)
        entries[key] = fold_event(entries.get(key), event)
        if last_event_at is None or event.observed_at > last_event_at:
            last_event_at = event.observed_at

    return _build_ledger(
        run_id=run_id, entries=entries.values(), last_event_at=last_event_at
    )


# ---------------------------------------------------------------------------
# Helpers — ledger assembly + row (de)serialization.
# ---------------------------------------------------------------------------


def _build_ledger(
    *,
    run_id: str,
    entries: Iterable[ModelPrLedgerEntry],
    last_event_at: str | None,
) -> ModelPrLedger:
    sorted_entries = tuple(sorted(entries, key=lambda e: (e.repo, e.pr_number)))
    return ModelPrLedger(
        run_id=run_id,
        last_event_at=last_event_at,
        entries=sorted_entries,
    )


def _row_from_entry(entry: ModelPrLedgerEntry) -> dict[str, object]:
    return entry.model_dump(mode="json")


def _entry_from_row(row: dict[str, object]) -> ModelPrLedgerEntry:
    return ModelPrLedgerEntry.model_validate(row)


__all__: list[str] = [
    "PROVENANCE_KIND",
    "PR_LEDGER_FRESHNESS_SLA_SECONDS",
    "EnumOrchestratorAction",
    "EnumPrLedgerConclusion",
    "EnumPrLedgerEventKind",
    "InMemoryPrLedgerStore",
    "ModelPrLedger",
    "ModelPrLedgerEntry",
    "ModelPrLedgerProvenance",
    "ModelPrLedgerSourceEvent",
    "ProjectionDatabasePrLedgerStore",
    "ProtocolPrLedgerStore",
    "apply_pr_ledger_event",
    "fold_event",
    "reconstruct_pr_ledger",
]
