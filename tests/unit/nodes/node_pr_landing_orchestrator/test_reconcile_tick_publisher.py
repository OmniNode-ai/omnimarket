# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reconciliation-tick fan-out (OMN-19829, T7's deferred item #4).

T7's PR (omnimarket#3001) built the orchestrator's consuming half of
``ModelPrLandingReconcileCommand`` (``_on_reconcile`` in ``orchestration/core.py``)
but nothing publishes it: "The reconciliation tick fan-out (runtime tick, then
one ModelPrLandingReconcileCommand per non-terminal row) is not built. The
handler handles the command and it is tested; the producer and its topic are
not." (ledger MSG 2026-09-27T08:21:03Z-pr-landing-w2-T7-83).

This module is the producer half: on a platform ``runtime-tick`` (the same
event-driven, config-gated-interval idiom ``node_dead_letter_prune_effect``
uses for its own schedule, never a polling loop), list every non-terminal row
(PARKED included, section 6) and publish one ``ModelPrLandingReconcileCommand``
per row. The row-listing port is unwired against a real table by default
(the ``pr_landing_workflow_state`` table itself is T7's own deferred item #3),
exactly like ``UnwiredHeadCheckClassifier`` -- a typed refusal, never a guess.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubMode,
    EnumPrLandingGithubOperation,
)
from omnimarket.nodes.node_pr_landing_orchestrator.handlers import (
    HandlerPrLandingOrchestrator,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_ingress import (
    ModelPrLandingReconcileCommand,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.core import (
    PrLandingOrchestratorConfig,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.ports import (
    PrLandingPortUnavailableError,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.reconcile_tick import (
    InMemoryPrLandingRowLister,
    ProtocolPrLandingRowLister,
    UnwiredPrLandingRowLister,
    build_reconcile_commands,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.row_store import (
    InMemoryPrLandingRowStore,
)
from omnimarket.nodes.node_pr_landing_reducer.handlers.handler_pr_landing_reducer import (
    HandlerPrLandingReducer,
)
from tests.unit.nodes.node_pr_landing_orchestrator._builders import (
    REPO,
    ArmingGate,
    FixedClassifier,
    answer,
    merged,
    only_request,
    pr_fact,
    prompt,
)

pytestmark = pytest.mark.unit

ENFORCE = PrLandingOrchestratorConfig(
    github_mode=EnumPrLandingGithubMode.ENFORCE,
    companion_exempt_repos=frozenset({REPO}),
    reconcile_tick_interval_seconds=1800,
)

_T0 = datetime(2026, 9, 27, 9, 0, 0, tzinfo=UTC)


def _tick(at: datetime) -> ModelRuntimeTick:
    """A ``ModelRuntimeTick`` at a given instant, a fresh id every call."""
    return ModelRuntimeTick(
        now=at,
        tick_id=uuid4(),
        sequence_number=0,
        scheduled_at=at,
        correlation_id=uuid4(),
        scheduler_id="test-scheduler",
        tick_interval_ms=1000,
    )


def _handler(
    *,
    store: InMemoryPrLandingRowStore,
    row_lister: ProtocolPrLandingRowLister | None = None,
) -> HandlerPrLandingOrchestrator:
    return HandlerPrLandingOrchestrator(
        reducer=HandlerPrLandingReducer(),
        arm_gate=ArmingGate(),
        classifier=FixedClassifier(),
        config=ENFORCE,
        store=store,
        row_lister=row_lister
        if row_lister is not None
        else InMemoryPrLandingRowLister(store),
    )


# --------------------------------------------------------------- RED: no producer


async def test_red_no_row_lister_is_wired_by_default() -> None:
    """RED-vs-exists-but-wrong: production wiring is a typed refusal, not a guess.

    Confirms the deferred state before this module existed: the default row
    lister (the one production wiring falls back to before the
    ``pr_landing_workflow_state`` table lands, T7's deferred item #3) refuses
    rather than silently listing nothing.
    """
    handler = HandlerPrLandingOrchestrator(
        reducer=HandlerPrLandingReducer(),
        arm_gate=ArmingGate(),
        classifier=FixedClassifier(),
        config=ENFORCE,
    )
    with pytest.raises(PrLandingPortUnavailableError):
        await UnwiredPrLandingRowLister().list_non_terminal()
    # And the handler's own default is exactly that unwired port: a tick against
    # a freshly constructed handler (no row_lister passed) refuses rather than
    # silently publishing nothing.
    with pytest.raises(PrLandingPortUnavailableError):
        await handler.handle(_tick(_T0))


# ------------------------------------------------------------- GREEN: fan-out


async def test_every_non_terminal_row_gets_one_reconcile_command_parked_included() -> (
    None
):
    store = InMemoryPrLandingRowStore()
    handler = _handler(store=store)

    # PR 1: pushed, never evaluated further -> OBSERVED.
    await handler.handle(prompt(pr_number=101, at=_T0))
    # PR 2: pushed, answered as a draft -> PARKED (section 6: PARKED included).
    read2 = only_request(await handler.handle(prompt(pr_number=102, at=_T0)))
    await handler.handle(answer(read2, pr_state=pr_fact(pr_number=102, draft=True)))
    # PR 3: pushed, answered as open and green -> CHECKS_PENDING then READY.
    read3 = only_request(await handler.handle(prompt(pr_number=103, at=_T0)))
    await handler.handle(answer(read3, pr_state=pr_fact(pr_number=103)))
    # PR 4: merged -> terminal, must be excluded from the fan-out.
    read4 = only_request(await handler.handle(prompt(pr_number=104, at=_T0)))
    await handler.handle(answer(read4, pr_state=pr_fact(pr_number=104)))
    await handler.handle(
        merged(_T0, event_id="m-104").model_copy(update={"pr_number": 104})
    )

    emitted = await handler.handle(_tick(_T0 + timedelta(minutes=1)))
    commands = [e for e in emitted if isinstance(e, ModelPrLandingReconcileCommand)]
    pr_numbers = sorted(c.pr_number for c in commands)
    assert pr_numbers == [101, 102, 103]
    assert all(c.repository == REPO for c in commands)
    assert all(c.landing_key == f"{REPO}#{c.pr_number}" for c in commands)


async def test_a_published_reconcile_command_drives_the_existing_handler_path() -> None:
    """The orchestrator's own consuming path (T7's ``_on_reconcile``) answers it."""
    store = InMemoryPrLandingRowStore()
    handler = _handler(store=store)
    read2 = only_request(await handler.handle(prompt(pr_number=202, at=_T0)))
    await handler.handle(
        answer(read2, pr_state=pr_fact(pr_number=202, draft=True))
    )  # PARKED

    emitted = await handler.handle(_tick(_T0 + timedelta(minutes=1)))
    (command,) = [e for e in emitted if isinstance(e, ModelPrLandingReconcileCommand)]

    consumed = await handler.handle(command)
    read = only_request(consumed)
    assert read.operation is EnumPrLandingGithubOperation.READ_PR_STATE


# -------------------------------------------------------------- interval gate


async def test_the_tick_gates_on_the_configured_interval_like_dead_letter_prune() -> (
    None
):
    store = InMemoryPrLandingRowStore()
    handler = _handler(store=store)
    await handler.handle(prompt(pr_number=301, at=_T0))

    first = await handler.handle(_tick(_T0))
    assert len(first) == 1

    # Well inside reconcile_tick_interval_seconds=1800: skipped, nothing published.
    second = await handler.handle(_tick(_T0 + timedelta(minutes=5)))
    assert second == []

    # Past the interval: fires again.
    third = await handler.handle(_tick(_T0 + timedelta(minutes=31)))
    assert len(third) == 1


# --------------------------------------------------------------- pure builder


def test_build_reconcile_commands_is_pure_and_deterministic() -> None:
    from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_workflow_row import (
        ModelPrLandingWorkflowRow,
    )

    rows = (
        ModelPrLandingWorkflowRow(
            repository=REPO,
            pr_number=1,
            landing_key=f"{REPO}#1",
            updated_at=_T0,
        ),
    )
    commands = build_reconcile_commands(rows, now=_T0, tick_id="fixed-tick")
    assert commands == build_reconcile_commands(rows, now=_T0, tick_id="fixed-tick")
    assert commands[0].tick_id == "fixed-tick"
    assert commands[0].requested_at == _T0
