# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review findings on the expired-response terminal (OMN-22822), each read against the code.

Three advisory findings were raised on the change that expires a replayed
inference response. Each test below fails on the head the findings were read
against and passes with the fix:

* the expiry comparison floored elapsed time to whole seconds, so a response at
  900.5 s against a 900 s bound was still acted on;
* the expiry terminal left the answering attempt out of ``escalation_history``,
  where every other failure terminal records its final attempt;
* the expiry terminal read the credential source from the previous response,
  not from the response that expired.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from omnibase_core.enums.enum_credential_source import EnumCredentialSource
from omnibase_core.models.delegation.wire.model_delegation_failed import (
    ModelDelegationFailed,
)
from pydantic import BaseModel

from omnimarket.enums.enum_delegation_acceptance import (
    EnumDelegationAcceptanceDecision,
    EnumDelegationAcceptanceReason,
)
from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers import (
    handler_delegation_workflow as handler_module,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    DelegationWorkflowState,
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)

pytestmark = pytest.mark.usefixtures("stub_provider_quota_reader")

_NS = 1_000_000_000
_BOUND_SECONDS = 900


def _recorded() -> ModuleType:
    """The recorded-pair helpers of the OMN-22822 test module, loaded by path."""
    path = Path(__file__).parent / "test_omn22822_expired_replayed_response.py"
    spec = importlib.util.spec_from_file_location("omn22822_recorded_pair", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _handle_at(
    monkeypatch: pytest.MonkeyPatch,
    elapsed_ns: int,
    *,
    credential_source: EnumCredentialSource | None = None,
    earlier_source: EnumCredentialSource | None = None,
) -> tuple[DelegationWorkflowState, list[BaseModel]]:
    recorded = _recorded()
    workflow: DelegationWorkflowState = recorded._recorded_workflow()
    workflow.inference_credential_source = earlier_source
    response: ModelInferenceResponseData = recorded._recorded_replay()
    if credential_source is not None:
        response = response.model_copy(update={"credential_source": credential_source})
    started = 1_800_000_000 * _NS
    workflow.started_at_ns = started
    monkeypatch.setattr(handler_module.time, "time_ns", lambda: started + elapsed_ns)
    handler = HandlerDelegationWorkflow(workflows={workflow.correlation_id: workflow})
    return workflow, handler.handle_inference_response(response)


@pytest.mark.unit
def test_a_response_a_fraction_of_a_second_past_the_bound_is_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, events = _handle_at(monkeypatch, _BOUND_SECONDS * _NS + 500_000_000)

    expired = workflow.expired_inference_response
    assert expired is not None
    assert expired.completion_bound_seconds == _BOUND_SECONDS
    assert expired.elapsed_seconds > _BOUND_SECONDS
    assert workflow.state == EnumDelegationState.FAILED
    assert [e for e in events if isinstance(e, ModelDelegationFailed)]


@pytest.mark.unit
def test_a_response_exactly_at_the_bound_is_still_acted_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, _ = _handle_at(monkeypatch, _BOUND_SECONDS * _NS)

    assert workflow.expired_inference_response is None


@pytest.mark.unit
def test_the_expired_attempt_is_recorded_in_the_terminal_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded_before = len(_recorded()._recorded_workflow().escalation_history)
    workflow, events = _handle_at(monkeypatch, 2 * _BOUND_SECONDS * _NS)

    last = workflow.escalation_history[-1]
    assert len(workflow.escalation_history) == recorded_before + 1
    assert last.acceptance_decision is EnumDelegationAcceptanceDecision.TERMINATE
    assert (
        last.acceptance_reason
        is EnumDelegationAcceptanceReason.COMPLETION_BOUND_EXPIRED
    )
    terminal = next(e for e in events if isinstance(e, ModelDelegationFailed))
    assert len(terminal.escalation_history) == recorded_before + 1
    assert terminal.escalation_count == workflow.escalation_count == 0


@pytest.mark.unit
def test_recording_the_expired_attempt_does_not_bank_its_spend_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _recorded()._recorded_workflow()
    workflow, _ = _handle_at(monkeypatch, 2 * _BOUND_SECONDS * _NS)

    assert workflow.cumulative_attempt_cost_usd == before.cumulative_attempt_cost_usd
    assert (
        workflow.cumulative_attempt_prompt_tokens
        == before.cumulative_attempt_prompt_tokens
    )
    assert (
        workflow.cumulative_attempt_completion_tokens
        == before.cumulative_attempt_completion_tokens
    )


@pytest.mark.unit
@pytest.mark.parametrize("earlier", [None, EnumCredentialSource.HOUSE])
def test_the_terminal_names_the_credential_of_the_expired_response(
    monkeypatch: pytest.MonkeyPatch, earlier: EnumCredentialSource | None
) -> None:
    _, events = _handle_at(
        monkeypatch,
        2 * _BOUND_SECONDS * _NS,
        credential_source=EnumCredentialSource.CUSTOMER_KEY,
        earlier_source=earlier,
    )

    terminal = next(e for e in events if isinstance(e, ModelDelegationFailed))
    assert terminal.credential_source == EnumCredentialSource.CUSTOMER_KEY


@pytest.mark.unit
def test_an_expired_response_with_no_credential_fact_does_not_inherit_an_older_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, events = _handle_at(
        monkeypatch,
        2 * _BOUND_SECONDS * _NS,
        credential_source=None,
        earlier_source=EnumCredentialSource.HOUSE,
    )

    terminal = next(e for e in events if isinstance(e, ModelDelegationFailed))
    assert terminal.credential_source is None
