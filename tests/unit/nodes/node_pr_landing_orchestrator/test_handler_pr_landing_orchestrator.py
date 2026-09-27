# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The orchestrator's own duties under plan 5.1 revision 1 (OMN-19829).

The reducer is the real node_pr_landing_reducer handler (T6). What these tests
hold is what the revision assigns to the orchestrator: the read sequence (section 6), the outbox (F6, F8, F9), one
effect in flight (R4), the arm gate before any arm, the completion bound per
state entry (R2a, R2b) and the dedup of agent-needed and terminal events (P4).
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from omnimarket.nodes.node_pr_arm_gate_compute.handlers.handler_arm_gate import (
    HandlerPrArmGate,
)
from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubMode,
    EnumPrLandingGithubOperation,
    ModelPrLandingGithubRequest,
)
from omnimarket.nodes.node_pr_landing_orchestrator.handlers import (
    HandlerPrLandingOrchestrator,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models import (
    EnumPrLandingAgentReason,
    EnumPrLandingIntentKind,
    EnumPrLandingState,
    ModelPrLandingAgentNeeded,
    ModelPrLandingMerged,
    ModelPrLandingTransitioned,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.core import (
    PrLandingOrchestratorConfig,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.outbox import (
    companion_command_id,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.row_store import (
    InMemoryPrLandingRowStore,
    PrLandingCasExhaustedError,
    decode_row,
    encode_row,
)
from omnimarket.nodes.node_pr_landing_orchestrator.state_codec import StateIoCodec
from omnimarket.nodes.node_pr_landing_reducer.handlers.handler_pr_landing_reducer import (
    HandlerPrLandingReducer,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    ModelPrLifecycleFixCommand,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_head_check_verdict import (
    EnumHeadCheckVerdict,
)
from tests.unit.nodes.node_pr_landing_orchestrator._builders import (
    HEAD_1,
    HEAD_2,
    KEY,
    NODE_ID,
    REPO,
    T0,
    ArmingGate,
    FixedClassifier,
    answer,
    check_run,
    later,
    merged,
    only_request,
    pr_fact,
    prompt,
    reconcile,
    requests_in,
)

pytestmark = pytest.mark.unit

# The checks path: this repo needs no companion, so evaluation reads the checks.
ENFORCE = PrLandingOrchestratorConfig(
    github_mode=EnumPrLandingGithubMode.ENFORCE,
    companion_exempt_repos=frozenset({REPO}),
)


def _handler(
    *,
    store: InMemoryPrLandingRowStore | None = None,
    verdict: EnumHeadCheckVerdict = EnumHeadCheckVerdict.GREEN,
    arming: bool = True,
    config: PrLandingOrchestratorConfig = ENFORCE,
) -> tuple[HandlerPrLandingOrchestrator, InMemoryPrLandingRowStore]:
    store = store if store is not None else InMemoryPrLandingRowStore()
    handler = HandlerPrLandingOrchestrator(
        reducer=HandlerPrLandingReducer(),
        arm_gate=ArmingGate() if arming else HandlerPrArmGate(),
        classifier=FixedClassifier(verdict),
        config=config,
        store=store,
    )
    return handler, store


async def _row(store: InMemoryPrLandingRowStore):  # type: ignore[no-untyped-def]
    row, _version = await store.load(KEY)
    assert row is not None
    return row


def _transitions(emitted: list[BaseModel]) -> list[ModelPrLandingTransitioned]:
    return [e for e in emitted if isinstance(e, ModelPrLandingTransitioned)]


async def _to_checks_pending(
    handler: HandlerPrLandingOrchestrator,
) -> ModelPrLandingGithubRequest:
    """Prompt, answer the read with an open PR; return the head-check read sent."""
    read = only_request(await handler.handle(prompt()))
    emitted = await handler.handle(answer(read, pr_state=pr_fact()))
    return only_request(emitted)


# ---------------------------------------------------------------- section 6


async def test_a_prompt_issues_one_read_pr_state_with_no_head_yet() -> None:
    handler, store = _handler()
    read = only_request(await handler.handle(prompt()))
    assert read.operation is EnumPrLandingGithubOperation.READ_PR_STATE
    # Reads are never dry_run: shadow mode needs the real snapshot (plan 5.5).
    assert read.mode is EnumPrLandingGithubMode.ENFORCE
    assert read.head_sha is None
    row = await _row(store)
    assert row.reads_issued == 1
    assert row.effect_in_flight is not None
    assert row.effect_in_flight.read_id == 1


async def test_a_second_prompt_while_a_read_is_in_flight_sends_nothing_more() -> None:
    handler, store = _handler()
    first = only_request(await handler.handle(prompt()))
    assert requests_in(await handler.handle(prompt(at=later(1)))) == []
    row = await _row(store)
    assert row.pending_read is True
    # The answer to read 1 frees the PR, and read 2 goes out before anything queued.
    emitted = await handler.handle(answer(first, pr_state=pr_fact()))
    second = only_request(emitted)
    assert second.operation is EnumPrLandingGithubOperation.READ_PR_STATE
    assert (await _row(store)).reads_issued == 2


async def test_the_snapshot_becomes_pushed_then_evaluation_keyed_by_the_read() -> None:
    handler, store = _handler()
    read = only_request(await handler.handle(prompt()))
    emitted = await handler.handle(answer(read, pr_state=pr_fact()))
    triggers = [t.trigger for t in _transitions(emitted)]
    assert triggers == ["pushed", "evaluated_checks_required"]
    row = await _row(store)
    assert row.landing is not None
    assert row.landing.state is EnumPrLandingState.CHECKS_PENDING
    assert row.landing.head_sha == HEAD_1
    assert row.landing.source_seq == 1 * 8 + 1
    assert row.reads_answered == 1
    assert row.pr_node_id == NODE_ID
    head_read = only_request(emitted)
    assert head_read.operation is EnumPrLandingGithubOperation.READ_HEAD_CHECKS
    assert head_read.head_sha == HEAD_1


async def test_a_redelivered_answer_is_dropped_whole() -> None:
    """F9: at-least-once delivery. The same completion twice moves nothing twice."""
    handler, store = _handler()
    read = only_request(await handler.handle(prompt()))
    completion = answer(read, pr_state=pr_fact())
    await handler.handle(completion)
    version = store.version(KEY)
    assert await handler.handle(completion) == []
    assert store.version(KEY) == version


async def test_the_workflows_own_companion_command_is_not_observed() -> None:
    handler, store = _handler()
    own = prompt(op="derive")
    assert await handler.handle(own) == []
    assert store.version(KEY) == 0


# ------------------------------------------------------ happy path, R4, P4


async def test_green_checks_arm_with_the_expected_head_then_merge_emits_one_terminal() -> (
    None
):
    handler, store = _handler()
    head_read = await _to_checks_pending(handler)
    emitted = await handler.handle(
        answer(head_read, check_runs=(check_run("ci / test", 111, 9001),))
    )
    arm = only_request(emitted)
    assert arm.operation is EnumPrLandingGithubOperation.ARM_AUTO_MERGE
    assert arm.head_sha == HEAD_1  # R4: the arm carries the head it was judged for
    assert arm.pr_node_id == NODE_ID
    assert [t.to_state for t in _transitions(emitted)] == [EnumPrLandingState.READY]
    emitted = await handler.handle(answer(arm))
    assert [t.to_state for t in _transitions(emitted)] == [EnumPrLandingState.ARMED]
    emitted = await handler.handle(merged(later(30)))
    terminals = [e for e in emitted if isinstance(e, ModelPrLandingMerged)]
    assert len(terminals) == 1
    assert terminals[0].episode == 0
    # A re-sent merged is not a second terminal (P4 per episode).
    assert await handler.handle(merged(later(31), event_id="merged-2")) == []
    row = await _row(store)
    assert row.terminal_episodes == (0,)


async def test_transitioned_seq_is_strictly_increasing_per_key() -> None:
    handler, _store = _handler()
    head_read = await _to_checks_pending(handler)
    arm = only_request(await handler.handle(answer(head_read)))
    emitted = await handler.handle(answer(arm))
    # pushed 1, evaluation 2, verdict_green 3, armed_confirmed 4
    assert [t.seq for t in _transitions(emitted)] == [4]


async def test_one_effect_in_flight_the_arm_waits_behind_a_read() -> None:
    """R4: nothing is sent for a PR while another effect for it is in flight."""
    handler, store = _handler()
    head_read = await _to_checks_pending(handler)
    await handler.handle(prompt(at=later(1)))  # wants a read while checks are read
    emitted = await handler.handle(answer(head_read))
    sent = only_request(emitted)
    assert sent.operation is EnumPrLandingGithubOperation.READ_PR_STATE
    row = await _row(store)
    assert row.landing is not None
    assert [i.kind for i in row.landing.outbox] == [EnumPrLandingIntentKind.GITHUB_ARM]


async def test_a_disarm_removes_the_unsent_arm_f6() -> None:
    """F6 and R4: a draft read while the arm is still queued never lets the arm out."""
    handler, store = _handler()
    head_read = await _to_checks_pending(handler)
    await handler.handle(prompt(at=later(1)))
    read = only_request(await handler.handle(answer(head_read)))
    emitted = await handler.handle(answer(read, pr_state=pr_fact(draft=True)))
    sent = only_request(emitted)
    assert sent.operation is EnumPrLandingGithubOperation.DISARM
    assert [t.to_state for t in _transitions(emitted)] == [EnumPrLandingState.PARKED]
    row = await _row(store)
    assert row.landing is not None
    assert row.landing.outbox == ()
    assert row.landing.armed is None


async def test_a_new_head_disarms_an_armed_pr_r4() -> None:
    handler, store = _handler()
    head_read = await _to_checks_pending(handler)
    arm = only_request(await handler.handle(answer(head_read)))
    await handler.handle(answer(arm))
    read = only_request(await handler.handle(prompt(at=later(5))))
    emitted = await handler.handle(answer(read, pr_state=pr_fact(head=HEAD_2)))
    ops = [r.operation for r in requests_in(emitted)]
    assert ops == [EnumPrLandingGithubOperation.DISARM]
    row = await _row(store)
    assert row.landing is not None
    assert row.landing.head_sha == HEAD_2
    # The head-check read for the new head waits behind the disarm.
    assert [i.kind for i in row.landing.outbox] == [
        EnumPrLandingIntentKind.GITHUB_READ_HEAD_CHECKS
    ]


# ----------------------------------------------------------------- arm gate


async def test_a_withheld_arm_is_never_queued_and_clears_armed() -> None:
    """The real arm gate under its default policy (report_only, kill switch) withholds."""
    handler, store = _handler(arming=False)
    head_read = await _to_checks_pending(handler)
    emitted = await handler.handle(answer(head_read))
    assert requests_in(emitted) == []
    (transition,) = _transitions(emitted)
    assert transition.to_state is EnumPrLandingState.READY
    assert all(
        i.kind is not EnumPrLandingIntentKind.GITHUB_ARM for i in transition.intents
    )
    row = await _row(store)
    assert row.landing is not None
    assert row.landing.armed is None
    assert row.landing.outbox == ()


async def test_dry_run_mutations_are_marked_dry_run() -> None:
    handler, _store = _handler(
        config=PrLandingOrchestratorConfig(companion_exempt_repos=frozenset({REPO}))
    )
    head_read = await _to_checks_pending(handler)
    arm = only_request(await handler.handle(answer(head_read)))
    assert arm.mode is EnumPrLandingGithubMode.DRY_RUN
    # A dry-run arm is not a confirmed arm: the row stays READY.
    emitted = await handler.handle(answer(arm))
    assert _transitions(emitted) == []


# ------------------------------------------------------------ completion bound


async def test_the_bound_expires_once_per_state_entry_and_pages_once() -> None:
    handler, store = _handler(arming=False)
    head_read = await _to_checks_pending(handler)
    await handler.handle(answer(head_read))  # READY, arm withheld
    emitted = await handler.handle(reconcile(later(16), tick="t1"))
    agents = [e for e in emitted if isinstance(e, ModelPrLandingAgentNeeded)]
    assert [a.reason for a in agents] == [EnumPrLandingAgentReason.STALLED]
    assert agents[0].from_state is EnumPrLandingState.READY
    row = await _row(store)
    assert row.landing is not None
    assert row.landing.state is EnumPrLandingState.NEEDS_AGENT
    # The same entry never expires twice, and (head, reason) pages once.
    read = only_request(emitted)
    await handler.handle(answer(read, not_modified=True))
    emitted = await handler.handle(reconcile(later(16 + 60 * 25), tick="t2"))
    assert [e for e in emitted if isinstance(e, ModelPrLandingAgentNeeded)] == []


async def test_parked_has_no_completion_bound_r2a() -> None:
    handler, store = _handler()
    read = only_request(await handler.handle(prompt()))
    await handler.handle(answer(read, pr_state=pr_fact(draft=True)))
    row = await _row(store)
    assert row.landing is not None
    assert row.landing.state is EnumPrLandingState.PARKED
    emitted = await handler.handle(reconcile(later(60 * 24 * 7)))
    assert _transitions(emitted) == []
    # The tick still reads a parked PR (section 6: PARKED included).
    assert only_request(emitted).operation is EnumPrLandingGithubOperation.READ_PR_STATE


async def test_real_red_pages_an_agent_once_per_head_and_reason() -> None:
    handler, store = _handler(verdict=EnumHeadCheckVerdict.PRODUCT_FAILED)
    head_read = await _to_checks_pending(handler)
    emitted = await handler.handle(answer(head_read))
    agents = [e for e in emitted if isinstance(e, ModelPrLandingAgentNeeded)]
    assert [(a.head_sha, a.reason) for a in agents] == [
        (HEAD_1, EnumPrLandingAgentReason.REAL_RED)
    ]
    row = await _row(store)
    assert len(row.agent_needed_sent) == 1


# ------------------------------------------------------------------ companion


async def test_a_companion_derive_goes_to_the_producer_with_a_uuid_command_id() -> None:
    handler, store = _handler(
        config=PrLandingOrchestratorConfig(github_mode=EnumPrLandingGithubMode.ENFORCE)
    )
    read = only_request(await handler.handle(prompt()))
    emitted = await handler.handle(answer(read, pr_state=pr_fact()))
    commands = [e for e in emitted if isinstance(e, ModelPrLifecycleFixCommand)]
    assert len(commands) == 1
    command = commands[0]
    assert command.op.value == "derive"
    row = await _row(store)
    assert row.landing is not None
    assert row.landing.state is EnumPrLandingState.COMPANION_PENDING
    # F5: the producer answers with this id, derived from the row's command id.
    assert row.landing.companion.command_id is not None
    assert command.correlation_id == companion_command_id(
        KEY, row.landing.companion.command_id
    )


# ------------------------------------------------------- compare-and-set, F8


class _RacingStore(InMemoryPrLandingRowStore):
    """Loses the first ``losses`` compare-and-sets, as if another worker won."""

    def __init__(self, losses: int) -> None:
        super().__init__()
        self.losses = losses

    async def compare_and_set(self, key, expected_version, row):  # type: ignore[no-untyped-def]
        if self.losses > 0:
            self.losses -= 1
            return False
        return await super().compare_and_set(key, expected_version, row)


async def test_a_lost_compare_and_set_reruns_the_leg_from_a_fresh_load() -> None:
    store = _RacingStore(losses=2)
    handler, _ = _handler(store=store)
    read = only_request(await handler.handle(prompt()))
    assert read.operation is EnumPrLandingGithubOperation.READ_PR_STATE
    assert store.version(KEY) == 1


async def test_an_exhausted_retry_raises_rather_than_reporting_success() -> None:
    store = _RacingStore(losses=99)
    handler, _ = _handler(store=store)
    with pytest.raises(PrLandingCasExhaustedError):
        await handler.handle(prompt())


# ------------------------------------------------------------------- codec


async def test_the_state_io_payload_round_trips_and_exposes_the_indexed_keys() -> None:
    handler, store = _handler()
    await _to_checks_pending(handler)
    row = await _row(store)
    raw = encode_row(row)
    assert '"state": "CHECKS_PENDING"' in raw
    assert '"in_flight": true' in raw
    assert decode_row(raw) == row
    codec = StateIoCodec()
    assert codec.decode(codec.encode(row)) == row
    assert codec.flush(KEY) is None  # nothing staged outside a state_io dispatch


def test_t0_is_timezone_aware() -> None:
    assert T0.tzinfo is not None


async def test_a_bound_expiry_in_companion_pending_pages_stalled_once() -> None:
    """AC3: COMPANION_PENDING past its 30-minute bound: one agent-needed, stalled."""
    handler, store = _handler(
        config=PrLandingOrchestratorConfig(github_mode=EnumPrLandingGithubMode.ENFORCE)
    )
    read = only_request(await handler.handle(prompt()))
    await handler.handle(answer(read, pr_state=pr_fact()))
    row = await _row(store)
    assert row.landing is not None
    assert row.landing.state is EnumPrLandingState.COMPANION_PENDING
    early = await handler.handle(reconcile(later(29), tick="t0"))
    assert [e for e in early if isinstance(e, ModelPrLandingAgentNeeded)] == []
    await handler.handle(answer(only_request(early), not_modified=True))
    emitted = await handler.handle(reconcile(later(31), tick="t1"))
    agents = [e for e in emitted if isinstance(e, ModelPrLandingAgentNeeded)]
    assert [(a.reason, a.from_state) for a in agents] == [
        (EnumPrLandingAgentReason.STALLED, EnumPrLandingState.COMPANION_PENDING)
    ]
    await handler.handle(answer(only_request(emitted), not_modified=True))
    again = await handler.handle(reconcile(later(33), tick="t2"))
    assert [e for e in again if isinstance(e, ModelPrLandingAgentNeeded)] == []
