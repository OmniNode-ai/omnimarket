# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reconciliation-tick fan-out: one command per non-terminal row (OMN-19829).

T7's PR (omnimarket#3001) built the whole consuming half of
``ModelPrLandingReconcileCommand`` -- ``_on_reconcile`` in
:mod:`.core` expires the completion bound, queues a stalled head-check
re-read, optionally verifies an open companion, and always issues a
conditional ``read_pr_state`` -- but named the producer as its own deferred
item #4: "The reconciliation tick fan-out (runtime tick, then one
ModelPrLandingReconcileCommand per non-terminal row) is not built. The
handler handles the command and it is tested; the producer and its topic are
not." (ledger MSG 2026-09-27T08:21:03Z-pr-landing-w2-T7-83).

This module is that producer's pure half: given the rows a lister returns,
build exactly one :class:`~..models.model_pr_landing_ingress.ModelPrLandingReconcileCommand`
per row whose state is non-terminal (:data:`.core.NON_TERMINAL_STATES` --
PARKED included, section 6 of the revision), or whose ``landing`` is still
``None`` (observed but not yet evaluated; ``_on_reconcile`` does not drop
those either). The event-driven trigger and its config-gated interval live on
:class:`~..handlers.handler_pr_landing_orchestrator.HandlerPrLandingOrchestrator`
itself, the same in-process elapsed-time idiom
``node_dead_letter_prune_effect`` uses for ``run_interval_seconds`` -- never a
polling loop, never a second bespoke daemon.

The listing port is intentionally unwired by default. Listing every row of
``pr_landing_workflow_state`` needs the table itself, which is T7's OWN
deferred item #3 ("the pr_landing_workflow_state table ... and its runtime
principal grant are not built"). Until wave 3 composes a real query,
:class:`UnwiredPrLandingRowLister` refuses rather than silently fanning out
zero commands -- the same "typed refusal, never a guess" idiom as
:class:`~.ports.UnwiredHeadCheckClassifier`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_ingress import (
    ModelPrLandingReconcileCommand,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_workflow_row import (
    ModelPrLandingWorkflowRow,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.core import (
    NON_TERMINAL_STATES,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.ports import (
    PrLandingPortUnavailableError,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.row_store import (
    InMemoryPrLandingRowStore,
)


@runtime_checkable
class ProtocolPrLandingRowLister(Protocol):
    """Every landing row worth reconciling right now."""

    async def list_non_terminal(self) -> tuple[ModelPrLandingWorkflowRow, ...]: ...


def is_non_terminal(row: ModelPrLandingWorkflowRow) -> bool:
    """Whether a row belongs in the tick's fan-out.

    Mirrors the guard ``_on_reconcile`` itself applies in :mod:`.core`: a row
    with no ``landing`` yet (observed but not evaluated) is included, and one
    whose state has left :data:`.core.NON_TERMINAL_STATES` (MERGED or CLOSED)
    is not.
    """
    return row.landing is None or row.landing.state in NON_TERMINAL_STATES


class InMemoryPrLandingRowLister:
    """Lists an :class:`InMemoryPrLandingRowStore`'s rows, for tests and local dispatch."""

    def __init__(self, store: InMemoryPrLandingRowStore) -> None:
        self._store = store

    async def list_non_terminal(self) -> tuple[ModelPrLandingWorkflowRow, ...]:
        rows = await self._store.all_rows()
        return tuple(row for row in rows if is_non_terminal(row))


class UnwiredPrLandingRowLister:
    """The listing port before wave 3 composes it: refuses, never guesses empty.

    A silent empty list is indistinguishable from "nothing needs
    reconciling" -- exactly the failure mode this whole feature exists to
    close (T7's report: "the reconciliation read never fires live"). Refusing
    keeps that failure loud instead of trading it for a quieter one.
    """

    async def list_non_terminal(self) -> tuple[ModelPrLandingWorkflowRow, ...]:
        msg = (
            "no row lister is wired for pr_landing_workflow_state: the table "
            "and its runtime principal grant are not built yet (OMN-19829 T7 "
            "deferred item #3); the reconciliation-tick fan-out cannot list "
            "rows it does not have a real query for"
        )
        raise PrLandingPortUnavailableError(msg)


def build_reconcile_commands(
    rows: tuple[ModelPrLandingWorkflowRow, ...],
    *,
    now: datetime,
    tick_id: str,
) -> tuple[ModelPrLandingReconcileCommand, ...]:
    """One command per row, deterministic and side-effect free.

    Every row in ``rows`` is included unconditionally: filtering to
    non-terminal rows is the lister's job (:func:`is_non_terminal`), not
    this function's, so a caller that already has a filtered set (a real
    ``WHERE state != ...`` query, once wave 3 builds one) does not pay for a
    second filter pass here.
    """
    return tuple(
        ModelPrLandingReconcileCommand.model_validate(
            {
                "repository": row.repository,
                "pr_number": row.pr_number,
                "requested_at": now,
                "tick_id": tick_id,
            }
        )
        for row in rows
    )


__all__: list[str] = [
    "InMemoryPrLandingRowLister",
    "ProtocolPrLandingRowLister",
    "UnwiredPrLandingRowLister",
    "build_reconcile_commands",
    "is_non_terminal",
]
