# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The walk contract: the walk, run against the fixture backend, ends in a delegated result.

The fixture backend is the scripted Tart transport of the walk node's own tests: nothing
leaves the test process. Each control below injects one named break into the fixture and
must read the walk as NOT ending in a delegated result, so a green golden is not a pass
that cannot fail.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.handlers.handler_onboarding_clean_vm_walk import (
    HandlerOnboardingCleanVmWalk,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.handlers.handler_walk_reading import (
    walk_ends_in_delegated_result,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.models.model_clean_vm_walk import (
    EnumWalkOutcome,
    EnumWalkStepKind,
    ModelRunnerResult,
    ModelWalkReceipt,
)
from tests.nodes.node_onboarding_clean_vm_walk.test_clean_vm_walk import (
    FAILED_LOG,
    SUCCESS_LOG,
    StubTartRunner,
    request,
)

pytestmark = pytest.mark.unit

_DELEGATE_LINE_PREFIX = '{"skill_name":"node_delegate_skill_orchestrator"'


def _walk(runner: StubTartRunner) -> ModelWalkReceipt:
    return HandlerOnboardingCleanVmWalk(runner=runner).handle(request())


def _log_without_delegate_result() -> str:
    """Every phase passes and the script exits 0, but no delegate run line exists."""
    return "\n".join(
        line
        for line in SUCCESS_LOG.splitlines()
        if not line.startswith(_DELEGATE_LINE_PREFIX)
    )


def _log_with_failed_delegate_result() -> str:
    return SUCCESS_LOG.replace('"status":"success"', '"status":"failed"')


def test_walk_contract_walk_ends_in_a_delegated_result() -> None:
    receipt = _walk(StubTartRunner())

    assert receipt.outcome == EnumWalkOutcome.PASSED
    assert receipt.delegated_run.status == "success"
    assert receipt.delegated_run.run_id == "success-1"
    assert receipt.delegated_run.backend == "local"
    assert receipt.seconds_to_first_delegated_run is not None
    assert receipt.vm_deleted
    assert receipt.dashboard.root_http_status == 200
    assert walk_ends_in_delegated_result(receipt)


def test_walk_contract_the_delegated_result_precedes_the_dashboard_probe() -> None:
    kinds = [step.kind for step in _walk(StubTartRunner()).steps]

    assert kinds.index(EnumWalkStepKind.GUEST_RUN_SCRIPT) < kinds.index(
        EnumWalkStepKind.GUEST_DASHBOARD_START
    )
    assert kinds[-2:] == [EnumWalkStepKind.TART_STOP, EnumWalkStepKind.TART_DELETE]


@pytest.mark.parametrize(
    "injected_break",
    ["failed_phase", "no_delegate_line", "delegate_failed", "script_exit_nonzero"],
)
def test_walk_contract_control_a_broken_walk_does_not_end_in_a_delegated_result(
    injected_break: str,
) -> None:
    runner = StubTartRunner()
    if injected_break == "failed_phase":
        runner.log = FAILED_LOG
    elif injected_break == "no_delegate_line":
        runner.log = _log_without_delegate_result()
    elif injected_break == "delegate_failed":
        runner.log = _log_with_failed_delegate_result()
    else:
        runner.scripted[EnumWalkStepKind.GUEST_RUN_SCRIPT] = [
            ModelRunnerResult(exit_code=3, stdout="preflight refused", stderr="")
        ]

    receipt = _walk(runner)

    assert receipt.outcome == EnumWalkOutcome.FAILED
    assert receipt.vm_deleted
    assert not walk_ends_in_delegated_result(receipt)


def test_walk_contract_control_a_passed_outcome_without_a_delegated_run_is_refused() -> (
    None
):
    """The predicate reads the delegated run itself, not the outcome word alone."""
    receipt = _walk(StubTartRunner())
    hollow = receipt.model_copy(
        update={
            "delegated_run": receipt.delegated_run.model_copy(
                update={"status": "not_run", "run_id": None}
            )
        }
    )

    assert hollow.outcome == EnumWalkOutcome.PASSED
    assert not walk_ends_in_delegated_result(hollow)
