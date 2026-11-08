# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain event tests for rebuild piece 9, landing (OMN-22922).

Two chains. The orchestrator chain is generated from the contract: each trigger that
enters the agent-needed state is read from the state machine, and the red-verdict path
is driven through the real orchestrator; the chain must end in the typed
agent-needed event naming the real red, and no arm request may have been made. The
decision chain injects a fault: a worker returns ``fix_submitted`` for a head that is
not the live head. The decision node must record the result invalid, count a violation,
take the next engine and never merge, with each tick carried over the overlay's bus.

Silence fails both: a chain that ends without the typed terminal is red.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubOperation,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models import (
    EnumPrLandingAgentReason,
    EnumPrLandingState,
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
from tests.chains.pr_landing.test_chain_pr_landing_orchestrator import _Rig, _types
from tests.unit.nodes.node_pr_landing_orchestrator._builders import (
    HEAD_1,
    KEY,
    answer,
    only_request,
    pr_fact,
    prompt,
)

pytestmark = pytest.mark.unit

ERROR_SCENARIO = "S08_fix_submitted_invalid"
GOLDEN_SCENARIO = "S07_fix_submitted_awaiting_ci"


async def drive_real_red_orchestrator() -> _Rig:
    rig = _Rig(verdict=EnumHeadCheckVerdict.PRODUCT_FAILED, arming=True)
    read = only_request(await rig.feed(prompt()))
    head_read = only_request(await rig.feed(answer(read, pr_state=pr_fact())))
    await rig.feed(answer(head_read))
    return rig


async def assert_real_red_chain(rig: _Rig, expected_triggers: list[str]) -> None:
    events = rig.recorder.events
    transitions = [
        e.payload for e in events if isinstance(e.payload, ModelPrLandingTransitioned)
    ]
    assert [t.trigger for t in transitions] == expected_triggers
    assert_chain(
        events,
        expected_event_types=_types(
            [ModelPrLandingTransitioned.__name__] * len(expected_triggers)
            + ["ModelPrLandingAgentNeeded"]
        ),
        terminal_fields={
            "reason": EnumPrLandingAgentReason.REAL_RED,
            "from_state": EnumPrLandingState.CHECKS_PENDING,
            "head_sha": HEAD_1,
        },
        correlation_id=KEY,
        bus_history_count=await rig.recorder.bus_history_count(),
    )
    assert all(
        r.operation is not EnumPrLandingGithubOperation.ARM_AUTO_MERGE
        for r in rig.requests
    )


async def test_error_orchestrator_chain_follows_the_contract_path() -> None:
    paths = contract_paths()
    assert "error:verdict_real_red" in paths
    expected = paths["error:verdict_real_red"]
    assert expected == ["pushed", "evaluated_checks_required", "verdict_real_red"]
    await assert_real_red_chain(await drive_real_red_orchestrator(), expected)


def test_every_error_path_the_contract_declares_is_named() -> None:
    """A new way into the agent-needed state shows here, so it gets a chain."""
    assert {k for k in contract_paths() if k.startswith("error:")} == {
        "error:companion_declined",
        "error:companion_error_budget_spent",
        "error:completion_bound_expired",
        "error:verdict_real_red",
    }


async def test_error_decision_chain_invalid_fix_is_recorded_and_never_merged(
    overlay: ModelLandingChainOverlay,
) -> None:
    world, bus, records = await run_world_chain(ERROR_SCENARIO, overlay)
    assert [r.actions for r in records] == [
        ("dispatch_worker acme/app#1 real_red",),
        ("dispatch_worker acme/app#1 real_red",),
    ]
    assert records[1].recorded == ("acme/app#1 invalid head_mismatch",)
    assert records[1].violations == 1
    assert world.prs["acme/app#1"].state.value == "open"
    assert not any(a.startswith("merge") for r in records for a in r.actions)
    assert len(bus.carried) == 2 * len(records)


# ------------------------------------------------------------ failing controls
async def test_control_orchestrator_chain_is_red_when_the_path_is_wrong() -> None:
    rig = await drive_real_red_orchestrator()
    wrong = ["pushed", "evaluated_checks_required", "verdict_green"]
    with pytest.raises(AssertionError):
        await assert_real_red_chain(rig, wrong)


@pytest.mark.parametrize(
    "mutation", ["invalid_fix_accepted", "red_counts_as_mergeable"]
)
async def test_control_decision_chain_is_red_under_named_mutation(
    mutation: str, overlay: ModelLandingChainOverlay
) -> None:
    with MUTATIONS[mutation](), pytest.raises(AssertionError):
        await run_world_chain(ERROR_SCENARIO, overlay)
    await run_world_chain(ERROR_SCENARIO, overlay)
