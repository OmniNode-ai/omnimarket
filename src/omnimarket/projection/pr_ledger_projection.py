# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-iteration PR ledger projection row (OMN-13321 / Enforcement Map F5).

What this is
------------
A *user-readable* per-PR ledger row materialized by
``HandlerPrLifecycleStateReducer`` on **every** sweep iteration. Where the
provenance-rich, ``run_id``-keyed derived ledger (OMN-12569,
``omnimarket.nodes.pr_ledger_native``) folds raw source events for replay, this
row answers the operator question the 2026-06-19 overnight sweep could not:
"what does the ledger say right now?" -- one durable, human-readable line per PR
per iteration:

    found_at . repo/pr . initial_state . action_taken . evidence . final_state .
    next_check_at

Why per-iteration
-----------------
OMN-12569 landed the derived ledger but it did not materialize a clean,
per-iteration row in practice. OMN-13321 hardens that: the reducer emits one row
per PR **each iteration** (not only at sweep end), keyed by
``(sweep_id, repo, pr_number, iteration)`` so two consecutive iterations both
produce rows and neither overwrites the other. The DoD probe is::

    select count(*) from pr_lifecycle_ledger_entries where sweep_id = '<id>';

which must be >= the open-PR count for that sweep.

Truth semantics
---------------
This projection is a *derived view* of reducer decisions; authoritative truth
remains GitHub state plus durable orchestrator receipts. The row is therefore
reconstructable: ``build_ledger_rows`` is a pure function of
``(sweep_id, iteration, found_at, freshness_sla_seconds, classified, intents)``.

Related:
    - OMN-13321: emit durable per-PR ledger projection every sweep iteration.
    - OMN-13316: merge-sweep & evidence-automation hardening epic (F5).
    - OMN-12569: orchestrator-owned derived PR ledger (hardened here).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


@runtime_checkable
class ProtocolTriageRecordLike(Protocol):
    """Structural view of a classified PR (avoids cross-node coupling).

    Matches node_pr_lifecycle_orchestrator's TriageRecord without importing
    it -- the reducer model must not depend on the orchestrator package
    (repo rule: promote shared types, never cross-import node packages),
    and a direct import here forms an import cycle
    (orchestrator handler -> this model -> orchestrator protocols).
    """

    @property
    def repo(self) -> str: ...

    @property
    def pr_number(self) -> int: ...

    @property
    def category(self) -> object: ...

    @property
    def block_reason(self) -> str: ...

    @property
    def failed_check_names(self) -> tuple[str, ...]: ...


@runtime_checkable
class ProtocolReducerIntentLike(Protocol):
    """Structural view of a reducer intent (avoids cross-node coupling)."""

    @property
    def repo(self) -> str: ...

    @property
    def pr_number(self) -> int: ...

    @property
    def intent(self) -> object: ...

    @property
    def reason(self) -> str: ...


# Durable projection surface (declared in contract.yaml projection_api / db_io;
# imported by call sites so the table/topic/key are never hardcoded twice).
PR_LEDGER_PROJECTION_TABLE = "pr_lifecycle_ledger_entries"
PR_LEDGER_PROJECTION_TOPIC = "onex.snapshot.projection.pr-lifecycle-ledger.v1"  # onex-topic-allow: projection snapshot topics use onex.snapshot.* prefix by convention
PR_LEDGER_PROJECTION_CONFLICT_KEY = "sweep_id,repo,pr_number,iteration"

# DT-003 freshness SLA: an entry is advisory for this window; next_check_at is
# found_at + this window. Declared here (not hardcoded at call sites) so the SLA
# is discoverable and consistent with the OMN-12569 ledger SLA.
PR_LEDGER_PROJECTION_FRESHNESS_SLA_SECONDS = 900


class EnumPrLedgerAction(StrEnum):
    """Action the reducer took for a PR this iteration (action_taken column)."""

    MERGE = "merge"
    FIX = "fix"
    SKIP = "skip"


class EnumPrLedgerFinalState(StrEnum):
    """Resulting state label after the reducer's decision (final_state column).

    These are the operator-readable outcomes for one iteration, derived
    deterministically from the reducer intent -- never inferred later from logs.
    """

    MERGE_REQUESTED = "merge_requested"
    FIX_DISPATCHED = "fix_dispatched"
    SKIPPED = "skipped"


# Reducer intent value -> (action_taken, final_state). Keyed by the intent's
# string value (EnumReducerIntent is a StrEnum) so the reducer model stays
# decoupled from the orchestrator node. A closed mapping: an intent value
# absent here is a programming error, surfaced by build_ledger_rows raising
# rather than silently writing a wrong row.
_INTENT_VALUE_TO_LEDGER: dict[
    str, tuple[EnumPrLedgerAction, EnumPrLedgerFinalState]
] = {
    EnumPrLedgerAction.MERGE.value: (
        EnumPrLedgerAction.MERGE,
        EnumPrLedgerFinalState.MERGE_REQUESTED,
    ),
    EnumPrLedgerAction.FIX.value: (
        EnumPrLedgerAction.FIX,
        EnumPrLedgerFinalState.FIX_DISPATCHED,
    ),
    EnumPrLedgerAction.SKIP.value: (
        EnumPrLedgerAction.SKIP,
        EnumPrLedgerFinalState.SKIPPED,
    ),
}


class ModelPrLedgerProjectionRow(BaseModel):
    """One durable, user-readable ledger row for a PR in a single iteration.

    Materialized into ``pr_lifecycle_ledger_entries`` (omnidash_analytics) via
    ``ProtocolProjectionDatabaseSync.upsert`` keyed by
    ``(sweep_id, repo, pr_number, iteration)``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sweep_id: str = Field(
        ...,
        min_length=1,
        description="Sweep run identifier (ledger partition key, == orchestrator run_id).",
    )
    iteration: int = Field(
        ...,
        ge=0,
        description="Monotonic per-sweep iteration index; distinct rows per iteration.",
    )
    found_at: datetime = Field(
        ...,
        description="When this PR was observed in this iteration.",
    )
    repo: str = Field(
        ..., min_length=1, description="Repo slug, e.g. 'OmniNode-ai/omnimarket'."
    )
    pr_number: int = Field(..., ge=1, description="GitHub PR number.")
    initial_state: str = Field(
        ...,
        min_length=1,
        description="Triage category at the start of this iteration (e.g. green, red).",
    )
    action_taken: EnumPrLedgerAction = Field(
        ..., description="Reducer action for this PR this iteration."
    )
    evidence: str = Field(
        default="",
        description="Why the action was taken (block reason / failed checks).",
    )
    final_state: EnumPrLedgerFinalState = Field(
        ..., description="Operator-readable outcome of the reducer decision."
    )
    next_check_at: datetime = Field(
        ...,
        description="When this PR is next due for examination (found_at + freshness SLA).",
    )

    def to_row(self) -> dict[str, object]:
        """Serialize to a projection-database row (JSON-mode; datetimes -> ISO)."""
        return self.model_dump(mode="json")


def _evidence_for(
    record: ProtocolTriageRecordLike, intent: ProtocolReducerIntentLike
) -> str:
    """Build the operator-readable evidence string for a ledger row.

    Prefers the reducer intent reason (it is the decision rationale); falls back
    to the triage block reason, then to a compact failed-check summary. Never
    invents evidence not present on the source records.
    """
    if intent.reason:
        return intent.reason
    if record.block_reason:
        return record.block_reason
    if record.failed_check_names:
        return "failed: " + ", ".join(record.failed_check_names)
    return ""


def build_ledger_rows(
    *,
    sweep_id: str,
    iteration: int,
    found_at: datetime,
    classified: Sequence[ProtocolTriageRecordLike],
    intents: Sequence[ProtocolReducerIntentLike],
    freshness_sla_seconds: int = PR_LEDGER_PROJECTION_FRESHNESS_SLA_SECONDS,
) -> tuple[ModelPrLedgerProjectionRow, ...]:
    """Build one ledger row per PR for a single iteration -- pure & deterministic.

    Every classified PR yields exactly one row; the intent for that PR supplies
    ``action_taken`` / ``final_state`` / ``evidence``. ``next_check_at`` is
    ``found_at + freshness_sla_seconds`` so consumers know when the row goes
    stale.

    Fail-fast (no defensive defaults):
      * a classified PR with no matching intent raises -- the reducer must emit an
        intent for every PR it classified;
      * an intent value absent from ``_INTENT_TO_LEDGER`` raises.

    Args:
        sweep_id: Sweep run identifier (partition key).
        iteration: Monotonic per-sweep iteration index.
        found_at: Observation timestamp for this iteration.
        classified: Triage records the reducer acted on this iteration.
        intents: Reducer intents (one per classified PR).
        freshness_sla_seconds: SLA window added to found_at for next_check_at.

    Returns:
        Tuple of ledger rows, ordered by (repo, pr_number) for stable output.
    """
    intents_by_pr: Mapping[tuple[str, int], ProtocolReducerIntentLike] = {
        (i.repo, i.pr_number): i for i in intents
    }
    next_check_at = found_at + timedelta(seconds=freshness_sla_seconds)

    rows: list[ModelPrLedgerProjectionRow] = []
    for record in classified:
        key = (record.repo, record.pr_number)
        intent = intents_by_pr.get(key)
        if intent is None:
            raise ValueError(
                "build_ledger_rows: no reducer intent for classified PR "
                f"{record.repo}#{record.pr_number}; the reducer must emit an "
                "intent for every PR it classifies"
            )
        intent_value = str(getattr(intent.intent, "value", intent.intent))
        mapping = _INTENT_VALUE_TO_LEDGER.get(intent_value)
        if mapping is None:
            raise ValueError(
                f"build_ledger_rows: unmapped reducer intent {intent.intent!r} "
                f"for {record.repo}#{record.pr_number}"
            )
        action_taken, final_state = mapping
        rows.append(
            ModelPrLedgerProjectionRow(
                sweep_id=sweep_id,
                iteration=iteration,
                found_at=found_at,
                repo=record.repo,
                pr_number=record.pr_number,
                initial_state=str(getattr(record.category, "value", record.category)),
                action_taken=action_taken,
                evidence=_evidence_for(record, intent),
                final_state=final_state,
                next_check_at=next_check_at,
            )
        )

    rows.sort(key=lambda r: (r.repo, r.pr_number))
    return tuple(rows)


__all__: list[str] = [
    "PR_LEDGER_PROJECTION_CONFLICT_KEY",
    "PR_LEDGER_PROJECTION_FRESHNESS_SLA_SECONDS",
    "PR_LEDGER_PROJECTION_TABLE",
    "PR_LEDGER_PROJECTION_TOPIC",
    "EnumPrLedgerAction",
    "EnumPrLedgerFinalState",
    "ModelPrLedgerProjectionRow",
    "build_ledger_rows",
]
