# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18223: the credential stamp survives the FAILURE terminal, not just success.

OMN-18196 landed the stamp at the effect boundary, where it is written from the
resolution the boundary actually performed -- on the success return AND on the
``except`` return. The orchestrator then copies it onto workflow state in
``_record_inference_response``. Both halves were correct and both were tested.

The value still did not reach the receipt on the failure path. The orchestrator
builds its terminal in four separate ``TerminalEmissionInputs(...)``
constructions and only ONE of them -- ``_gate_terminal_inputs``, reached only
after a quality gate ran, i.e. only after the inference SUCCEEDED -- forwarded
``route`` / ``provider`` / ``credential_source``. The "no escalation possible:
terminal FAILED" branch calls ``_record_inference_response`` and then, one line
later, hand-rolls a ``TerminalEmissionInputs`` that never reads those three
fields back out. They default to ``None``, so the fact recorded a line earlier
was silently dropped.

WHAT THAT COST, measured on the lab. C7 chain run 34702707401, golden delegation
a2984b1d-5dbe-4e7b-98d0-c170e889f71d: the customer's registered key resolved by
reference, the effect called OpenRouter, and the vendor returned an empty
``choices`` array. ``gateway_workflows`` row 2280005b carries the routed model in
``terminal_model_used``, which only happens after the credential resolved and the
call went out -- and the receipt recorded no credential at all. Three C7 checks
red on a fact that is true, and the M2 bar's leg 6 reported "the credential did
not resolve by reference at the effect boundary" about a credential that did.

A vendor decline of a resolved key was therefore indistinguishable from a key
that never resolved. Axiom 9's audit trail exists to tell exactly those two
apart, and the runs worth auditing are the ones that failed.

WHY THE EXISTING TESTS DID NOT SEE IT. ``omninode_infra``'s
``test_a_vendor_decline_of_a_resolved_key_reds_only_the_inference_leg`` pins the
GRADER against a synthetic receipt that already carries the field. omnimarket's
``test_omn18196_credential_source_stamp`` pins the PRODUCER's first hop, the
effect boundary, and stops there. Nothing exercised the hop between them. These
tests do: the REAL effect handler produces the response (only the vendor's HTTP
call is stubbed), and the REAL orchestrator turns it into the terminal that is
asserted on.

The structural test at the bottom is the part that keeps this closed. Two case
tests would pass again the moment someone adds a fifth terminal construction
site, which is exactly how the fourth one came to be missing.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest
from omnibase_core.models.delegation.wire import (
    EnumCredentialSource,
    ModelInferenceIntent,
)

from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers import (
    handler_delegation_workflow as orchestrator_module,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    HandlerInferenceIntent,
)

pytestmark = pytest.mark.unit

_EFFECT_MODULE = (
    "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent"
)

# A minted tenant-credential reference: ``cred_<tenant>_<provider>_<uuid4hex>``.
# Shape authority is omnimarket.tenant_credential_ref.
_CUSTOMER_REF = "cred_acme_openrouter_" + "0" * 32

_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
_ROUTE = "byok-openrouter"
_PROVIDER = "openrouter"

# The live lab shape: the provider metered the call and reported usage, then
# returned no choices at all. Parsed by ``_call_llm`` into an InferenceUsageError
# carrying the served counts -- which is why the terminal has real tokens to
# report on a call that produced nothing.
_VENDOR_EMPTY_CHOICES_PAYLOAD: dict[str, object] = {
    "id": "gen-omn18223",
    "choices": [],
    "usage": {"prompt_tokens": 311, "completion_tokens": 0, "total_tokens": 311},
}

# Higher than any task class's configured ceiling, so the escalation COMPUTE
# answers "ladder exhausted" and the FAILED terminal is emitted rather than a
# further routing intent. This is a workflow that already climbed, not a knob
# that changes what the branch under test does.
_LADDER_EXHAUSTED = 99


def _make_request(correlation_id: UUID) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Say ok.",
        task_type="research",  # type: ignore[arg-type]
        correlation_id=correlation_id,
        emitted_at=datetime.now(UTC),
    )


def _make_routing_decision(correlation_id: UUID) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=correlation_id,
        task_type="research",
        selected_model=_MODEL,
        selected_backend_id=uuid5(NAMESPACE_DNS, "omninode.ai/backends/openrouter"),
        endpoint_url="https://openrouter.ai/api/v1/chat/completions",
        cost_tier="low",
        tier_name="free_cloud",
        max_context_tokens=65536,
        max_tokens=512,
        system_prompt="You are a helpful assistant.",
        rationale="Task 'research' routed onto the tenant's own provider key.",
    )


def _make_intent(correlation_id: UUID, **kwargs: object) -> ModelInferenceIntent:
    defaults: dict[str, object] = {
        "base_url": "https://openrouter.ai/api/v1/chat/completions",
        "model": _MODEL,
        "system_prompt": "You are a helpful assistant.",
        "prompt": "Say ok.",
        "max_tokens": 512,
        "temperature": 0.7,
        "timeout_seconds": 30.0,
        "correlation_id": correlation_id,
        "api_key_ref": _CUSTOMER_REF,
        "route": _ROUTE,
        "provider": _PROVIDER,
    }
    defaults.update(kwargs)
    return ModelInferenceIntent(**defaults)  # type: ignore[arg-type]


def _real_effect_response(
    correlation_id: UUID,
    *,
    resolved_key: str | None,
    vendor_payload: dict[str, object] | None = None,
    **intent_kwargs: object,
):
    """Run the REAL effect handler; stub only the vendor's HTTP call.

    The credential resolution, the classification, the empty-choices parse and
    the error-response construction are all the shipping code paths. Only the
    network hop and the secret store are replaced.
    """
    handler = HandlerInferenceIntent()
    mock_response = MagicMock()
    mock_response.json.return_value = vendor_payload or _VENDOR_EMPTY_CHOICES_PAYLOAD
    mock_response.raise_for_status.return_value = None

    with (
        patch(f"{_EFFECT_MODULE}._resolve_api_key", return_value=resolved_key),
        patch("httpx.Client") as mock_client_cls,  # onex-allow-faked-boundary
    ):
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value = mock_client
        return handler.handle(_make_intent(correlation_id, **intent_kwargs))


def _drive_to_failed_terminal(
    correlation_id: UUID,
    response,
    *,
    exhaust_ladder: bool = False,
) -> ModelDelegationResult:
    """Feed a real effect response to the real orchestrator; return its terminal."""
    handler = HandlerDelegationWorkflow()
    handler.handle_delegation_request(_make_request(correlation_id))
    handler.handle_routing_decision(_make_routing_decision(correlation_id))
    if exhaust_ladder:
        handler.workflows[correlation_id].escalation_count = _LADDER_EXHAUSTED
    events = handler.handle_inference_response(response)

    assert handler.workflows[correlation_id].state == EnumDelegationState.FAILED, (
        "precondition: this shape must reach the FAILED terminal, not escalate — "
        f"state was {handler.workflows[correlation_id].state}"
    )
    terminals = [e for e in events if isinstance(e, ModelDelegationResult)]
    assert len(terminals) == 1, f"expected exactly one terminal, got {events!r}"
    return terminals[0]


class TestTheEffectStillReportsTheCredentialOnADecline:
    """Positive control for the two terminal tests below.

    If the effect ever stopped stamping the failure return, the terminal
    assertions would red and point at the orchestrator, which would be the wrong
    place to look. These pin the input to the hop under test, so a failure there
    names the effect instead.
    """

    def test_a_vendor_empty_choices_decline_leaves_the_effect_with_the_customer_key(
        self,
    ) -> None:
        response = _real_effect_response(uuid4(), resolved_key="sk-customer-resolved")

        assert "empty choices array" in response.error_message
        assert response.credential_source is EnumCredentialSource.CUSTOMER_KEY
        assert response.route == _ROUTE
        assert response.provider == _PROVIDER
        # The vendor metered the call. Real tokens on a call that produced no
        # content is what proves the request actually went out.
        assert response.prompt_tokens > 0

    def test_a_refusal_for_want_of_a_credential_leaves_the_effect_with_none(
        self,
    ) -> None:
        response = _real_effect_response(
            uuid4(),
            resolved_key=None,
            expected_credential_source=EnumCredentialSource.CUSTOMER_KEY,
        )

        assert "delegation.credential.unresolved" in response.error_message
        assert response.credential_source is EnumCredentialSource.NONE


class TestTheCredentialStampReachesTheFailureTerminal:
    """The hop that was broken: effect response -> emitted terminal."""

    def test_a_vendor_decline_of_a_resolved_key_still_names_the_customer_key(
        self,
    ) -> None:
        """The live 34702707401 shape, end to end through the real producers.

        An empty ``choices`` array is a NON-retryable inference error, so this
        lands on the "no escalation possible: terminal FAILED" branch with no
        ladder manipulation at all — the exact construction site that dropped
        the field.

        Before the fix this asserted ``None``: a receipt saying nothing about a
        credential that had demonstrably resolved and paid for a vendor call.
        """
        cid = uuid4()
        response = _real_effect_response(cid, resolved_key="sk-customer-resolved")
        terminal = _drive_to_failed_terminal(cid, response)

        assert terminal.credential_source is EnumCredentialSource.CUSTOMER_KEY, (
            "the vendor declined a key that resolved; a terminal that records no "
            "credential makes that indistinguishable from a key that never "
            "resolved, which is the one distinction this field exists for"
        )
        assert terminal.route == _ROUTE
        assert terminal.provider == _PROVIDER

    def test_a_refusal_for_want_of_a_credential_names_none_not_nothing(self) -> None:
        """The refusal shape terminalises through the same site, and must too.

        A credential refusal is a retryable class by the marker vocabulary, so
        the ladder is exhausted here to reach the terminal rather than a further
        routing intent. ``none`` is a positive finding — the call was refused
        because nothing resolved — and dropping it reports the refusal as a run
        with no credential story at all.
        """
        cid = uuid4()
        response = _real_effect_response(
            cid,
            resolved_key=None,
            expected_credential_source=EnumCredentialSource.CUSTOMER_KEY,
        )
        terminal = _drive_to_failed_terminal(cid, response, exhaust_ladder=True)

        assert terminal.credential_source is EnumCredentialSource.NONE

    def test_a_house_credential_decline_names_house_not_the_customer_key(self) -> None:
        """The field is observed NOT reading ``customer_key`` on this path too.

        Without this, every failure-terminal observation of the field reads
        ``customer_key``, which is indistinguishable from a forwarding fix that
        hardcoded the value it was asked to produce.
        """
        cid = uuid4()
        response = _real_effect_response(
            cid,
            resolved_key="sk-house-resolved",
            api_key_ref="llm.openrouter.api_key",
        )
        terminal = _drive_to_failed_terminal(cid, response)

        assert terminal.credential_source is EnumCredentialSource.HOUSE


class TestNoTerminalConstructionSiteCanOmitTheStamp:
    """The structural guard, and the reason this ticket is not just a one-liner.

    The defect was not a wrong value. It was a construction site that forgot a
    field, in a module that builds the same DTO in four places. Case tests
    cannot see the fifth site someone adds next quarter; this does.
    """

    @staticmethod
    def _terminal_construction_sites() -> list[ast.Call]:
        source = Path(inspect.getsourcefile(orchestrator_module) or "").read_text(
            encoding="utf-8"
        )
        return [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "TerminalEmissionInputs"
        ]

    def test_the_module_still_has_more_than_one_construction_site(self) -> None:
        """Positive control: a zero here would make the next test vacuously green."""
        assert len(self._terminal_construction_sites()) > 1

    @pytest.mark.parametrize("field", ["route", "provider", "credential_source"])
    def test_every_construction_site_forwards_the_provenance_fields(
        self, field: str
    ) -> None:
        missing = [
            call.lineno
            for call in self._terminal_construction_sites()
            if field not in {kw.arg for kw in call.keywords if kw.arg is not None}
        ]
        assert not missing, (
            f"TerminalEmissionInputs built without {field!r} at line(s) "
            f"{missing} of handler_delegation_workflow.py. Every terminal "
            "carries what the workflow knows about the call that produced it; "
            "a site that omits the field reports 'no credential fact' for a run "
            "whose credential the effect boundary already resolved and recorded."
        )
