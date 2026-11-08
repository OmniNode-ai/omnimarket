# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain event tests for rebuild piece 9, landing (OMN-22922).

Two chains. The orchestrator chain is generated from the orchestrator contract: the
shortest trigger path to ``MERGED`` is read from the state machine, driven through the
real orchestrator and reducer, and the events on the bus must carry exactly those
triggers in order and end in the merged terminal. The decision chain takes a red head
through dispatch, a fix, green CI and the pinned merge, with every tick's snapshot and
decision carried over the overlay's bus (in memory, or a broker).

The control at the end of each chain runs a named mutation of the decision node and
requires the chain to fail.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubOperation,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models import (
    ModelPrLandingTransitioned,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_head_check_verdict import (
    EnumHeadCheckVerdict,
)
from tests.chain.landing_chain import (
    MUTATIONS,
    ModelLandingChainOverlay,
    contract_paths,
    run_world_chain,
)
from tests.chains.chain_assert import assert_chain
from tests.chains.pr_landing.test_chain_pr_landing_orchestrator import (
    _Rig,
    _types,
)
from tests.unit.nodes.node_pr_landing_orchestrator._builders import (
    KEY,
    PR,
    REPO,
    answer,
    check_run,
    later,
    merged,
    only_request,
    pr_fact,
    prompt,
)

pytestmark = pytest.mark.unit

GOLDEN_SCENARIO = "S07_fix_submitted_awaiting_ci"


async def drive_golden_orchestrator() -> _Rig:
    """Fire the contract's golden triggers: pushed, evaluated, green, armed, merged."""
    rig = _Rig(verdict=EnumHeadCheckVerdict.GREEN, arming=True)
    read = only_request(await rig.feed(prompt()))
    head_read = only_request(await rig.feed(answer(read, pr_state=pr_fact())))
    arm = only_request(
        await rig.feed(answer(head_read, check_runs=(check_run("ci", 7, 70),)))
    )
    assert arm.operation is EnumPrLandingGithubOperation.ARM_AUTO_MERGE
    await rig.feed(answer(arm))
    await rig.feed(merged(later(20)))
    return rig


async def assert_golden_orchestrator(rig: _Rig, expected_triggers: list[str]) -> None:
    events = rig.recorder.events
    transitions = [
        e.payload for e in events if isinstance(e.payload, ModelPrLandingTransitioned)
    ]
    assert [t.trigger for t in transitions] == expected_triggers
    assert_chain(
        events,
        expected_event_types=_types(
            [ModelPrLandingTransitioned.__name__] * len(expected_triggers)
            + ["ModelPrLandingMerged"]
        ),
        terminal_fields={
            "repository": REPO,
            "pr_number": PR,
            "episode": 0,
            "seq": len(expected_triggers),
        },
        correlation_id=KEY,
        bus_history_count=await rig.recorder.bus_history_count(),
    )


async def test_golden_orchestrator_chain_follows_the_contract_path() -> None:
    expected = contract_paths()["golden"]
    # The contract's golden path is the one the driver fires; a contract change that
    # moves the path fails here before it can pass unseen.
    assert expected == [
        "pushed",
        "evaluated_checks_required",
        "verdict_green",
        "armed_confirmed",
        "merged",
    ]
    await assert_golden_orchestrator(await drive_golden_orchestrator(), expected)


async def test_golden_decision_chain_red_head_to_pinned_merge(
    overlay: ModelLandingChainOverlay,
) -> None:
    world, bus, records = await run_world_chain(GOLDEN_SCENARIO, overlay)
    assert [r.actions for r in records] == [
        ("dispatch_worker acme/app#1 real_red",),
        ("merge acme/app#1 h2",),
        (),
    ]
    assert records[1].recorded == ("acme/app#1 fix_submitted none",)
    assert world.prs["acme/app#1"].state.value == "merged"
    # one facts message and one decision message per tick crossed the bus
    assert len(bus.carried) == 2 * len(records)
    assert set(bus.carried) == {bus.topic_facts, bus.topic_decision}


# ------------------------------------------------------------ failing controls
async def test_control_orchestrator_chain_is_red_when_a_trigger_is_missing() -> None:
    """The chain assertion fails when the contract path and the run disagree."""
    rig = await drive_golden_orchestrator()
    wrong = [t for t in contract_paths()["golden"] if t != "armed_confirmed"]
    with pytest.raises(AssertionError):
        await assert_golden_orchestrator(rig, wrong)


@pytest.mark.parametrize("mutation", ["merge_never_lands", "red_counts_as_mergeable"])
async def test_control_decision_chain_is_red_under_named_mutation(
    mutation: str, overlay: ModelLandingChainOverlay
) -> None:
    with MUTATIONS[mutation](), pytest.raises(AssertionError):
        await run_world_chain(GOLDEN_SCENARIO, overlay)
    # and green again once the mutation is gone
    await run_world_chain(GOLDEN_SCENARIO, overlay)
