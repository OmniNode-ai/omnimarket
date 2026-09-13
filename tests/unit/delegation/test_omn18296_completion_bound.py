# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The declared completion bound, its terminal, and the client that reads it (OMN-18296).

Cloud delegation ``16eafedc-199c-44c2-a2cb-b9e535839bb9`` was submitted to the
lab lane at 2026-09-13T09:55:04Z. At 09:57:40Z the ``omninode-runtime-effects``
pod was recreated by a concurrent sanctioned re-apply, with that delegation's
inference command in flight. The command's consumer offset was already committed
(``TOTAL-LAG 0``, current offset 12 of 12), so the record was never redelivered
and no inference response — success or failure — was ever published: the bus
carried 12 requests and 11 responses, and the missing one is this correlation.
The FSM row stayed ``ROUTED`` with ``in_flight = TRUE``, and the gateway row
stayed ``published`` with ``completed_at NULL``, indefinitely.

Nothing in the system bounded that. The runtime's give-up TTL was an
environment-variable default in another repository and wrote only to the row;
the client's patience was a hardcoded 300 seconds unrelated to it. This suite
pins the three halves of the fix: the bound is declared in the contract, the
terminal it produces is typed and legible to the gateway, and the client waits
for the declared number rather than one of its own.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from omnibase_core.enums.enum_core_error_code import EnumCoreErrorCode
from omnibase_core.errors.model_onex_error import ModelOnexError
from omnibase_core.models.delegation.wire.model_delegation_failed import (
    ModelDelegationFailed,
)
from pydantic import SecretStr

from omnimarket.cli.cli_cloud import cloud_delegate
from omnimarket.cloud.completion_bound import read_declared_completion_bound
from omnimarket.cloud.transport_cloud_delegation import (
    CLOUD_DELEGATION_WORKFLOW_TYPE,
    TransportCloudDelegation,
)
from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.nodes.node_delegation_orchestrator.state_codec import StateIoCodec

_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_delegation_orchestrator"
    / "contract.yaml"
)
_WORKFLOW_ID = "16eafedc-199c-44c2-a2cb-b9e535839bb9"
_CORRELATION = "a2fe0848-4b4b-462e-b633-c5f9559afee5"
_TENANT = "cacacbb1-0e64-4521-9712-ed02ee799907"

# The gateway's own attribution grammar
# (omninode_infra docker/onex-api/workflow_failure_attribution.py). Reproduced
# here rather than imported because that repository is not a dependency of this
# one; a terminal whose attribution does not match it is reported downstream as
# carrying no failure class at all, which is how a typed refusal reaches a
# customer as an unexplained failure.
_ATTRIBUTION_GRAMMAR = (
    r"^(?P<failure_class>[A-Z][A-Za-z0-9_]*(?:Error|Exception))"
    r"(?::[ ](?P<failure_code>ONEX_[A-Z0-9_]+))?$"
)


def _abandoned_payload() -> str:
    """A ``delegation_workflow_state`` payload in the shape the live row had."""
    return json.dumps(
        {
            "state": "ROUTED",
            "in_flight": True,
            "tenant_id": _TENANT,
            "correlation_id": _CORRELATION,
            "started_at_ns": 1789293304198594138,
            "escalation_count": 0,
            "compliance_attempts": 1,
            "context_pack_hash": "",
            "current_tier_name": "tenant_overlay",
            "request": {
                "prompt": "summarise this",
                "task_type": "summarization",
                "tenant_id": _TENANT,
                "correlation_id": _CORRELATION,
                "emitted_at": "2026-09-13T09:55:04.089592Z",
            },
            "routing_decision": {
                "cost_tier": "tenant_byok",
                "rationale": "tenant overlay",
                "task_type": "summarization",
                "tier_name": "tenant_overlay",
                "max_tokens": 65536,
                "max_context_tokens": 8192,
                "api_key_ref": "cred_x",
                "endpoint_url": "https://openrouter.ai/api/v1/chat/completions",
                "correlation_id": _CORRELATION,
                "selected_model": "nvidia/nemotron-3-ultra-550b-a55b:free",
                "selected_backend_id": "a83745fa-d9ee-5239-a363-f9349f3bff08",
                "selected_backend_ref": "byok-openrouter",
                "system_prompt": "You are a summarization assistant.",
            },
        }
    )


@pytest.mark.unit
def test_the_contract_declares_the_bound_the_runtime_enforces() -> None:
    """AC2: the bound is a typed contract field, not a number in one script."""
    block = yaml.safe_load(_CONTRACT.read_text())["completion_bound"]
    assert block["max_wall_seconds"] > 0
    assert block["on_runtime_restart"] == "terminalise_failed"
    # The declared class must be a real member of this node's failure
    # vocabulary, or the terminal names a class nothing else in the system
    # recognises.
    assert (
        EnumDelegationFailureClass(block["failure_class"])
        is EnumDelegationFailureClass.RUNTIME_RESTART_DURING_DELEGATION
    )


@pytest.mark.unit
def test_the_codec_builds_a_typed_restart_terminal_from_an_abandoned_row() -> None:
    """AC3: the give-up is a real, typed, gateway-legible terminal event."""
    import re

    built = StateIoCodec().build_abandoned_terminal(
        correlation_id=_CORRELATION,
        tenant_id=_TENANT,
        state="ROUTED",
        payload_json=_abandoned_payload(),
        failure_class=EnumDelegationFailureClass.RUNTIME_RESTART_DURING_DELEGATION.value,
        failure_code="ONEX_MARKET_DELEGATION_RUNTIME_RESTART",
        max_wall_seconds=900,
    )
    assert built is not None
    module, class_name, payload = built
    assert class_name == "ModelDelegationFailed"
    assert module.endswith("model_delegation_failed")

    terminal = ModelDelegationFailed.model_validate(payload)
    assert terminal.quality_passed is False
    assert terminal.tenant_id == _TENANT
    assert terminal.task_type == "summarization"
    # Attribution the gateway can actually parse into a class and a code.
    assert terminal.terminal_failure_reason is not None
    match = re.match(_ATTRIBUTION_GRAMMAR, terminal.terminal_failure_reason)
    assert match is not None, terminal.terminal_failure_reason
    assert match.group("failure_class") == "RuntimeRestartDuringDelegationError"
    assert match.group("failure_code") == "ONEX_MARKET_DELEGATION_RUNTIME_RESTART"
    # Nothing was served, so nothing is claimed.
    assert terminal.content == ""
    assert terminal.total_tokens == 0
    assert "900s" in terminal.failure_reason


@pytest.mark.unit
def test_an_undecodable_row_is_left_alone_rather_than_closed_on_a_guess() -> None:
    """A payload shape this build does not understand is not terminalised."""
    assert (
        StateIoCodec().build_abandoned_terminal(
            correlation_id=_CORRELATION,
            tenant_id=_TENANT,
            state="ROUTED",
            payload_json='{"not": "a workflow state"}',
            failure_class="runtime_restart_during_delegation",
            failure_code=None,
            max_wall_seconds=900,
        )
        is None
    )


@pytest.mark.unit
def test_the_client_waits_for_the_declared_bound_not_a_number_of_its_own() -> None:
    """AC4: ``--timeout`` defaults to the contract's bound, not a CLI constant."""
    declared = read_declared_completion_bound()
    contract_value = yaml.safe_load(_CONTRACT.read_text())["completion_bound"]
    assert declared.max_wall_seconds == contract_value["max_wall_seconds"]

    timeout_option = next(
        param for param in cloud_delegate.params if param.name == "timeout"
    )
    assert timeout_option.default is None, (
        "a hardcoded default here is a second, competing bound — the platform's "
        "declared one is the only one a caller should be waiting for"
    )


@pytest.mark.unit
def test_the_timeout_error_says_whose_bound_was_spent() -> None:
    """AC4: still non-terminal AT the platform's own bound is a different fact.

    A caller-chosen budget running out means "wait longer". The platform's own
    declared bound running out means the runtime owed a terminal and did not
    deliver one. The typed error must distinguish them, because the customer's
    next move differs.
    """

    class _Clock:
        def __init__(self) -> None:
            self.now = 0.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "workflow_id": _WORKFLOW_ID,
                "workflow_type": CLOUD_DELEGATION_WORKFLOW_TYPE,
                "status": "published",
                "envelope_id": str(uuid.uuid4()),
                "correlation_id": _CORRELATION,
                "command_topic": "onex.cmd.delegation.inference.v1",
                "submitted_at": "2026-09-13T09:55:04Z",
                "updated_at": "2026-09-13T09:55:04Z",
            },
        )

    def _poll(**extra: Any) -> ModelOnexError:
        clock = _Clock()
        client = TransportCloudDelegation(
            base_url="https://dev.api.omninode.ai",
            api_key=SecretStr("onxk_testkey"),
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),  # type: ignore[arg-type]
        )
        with pytest.raises(ModelOnexError) as excinfo:
            client.poll_until_terminal(
                _WORKFLOW_ID,
                deadline_seconds=5.0,
                sleep_fn=clock.sleep,
                monotonic_fn=clock.monotonic,
                **extra,
            )
        return excinfo.value

    declared_source = "the completion bound declared by node_delegation_orchestrator"
    with_source = _poll(deadline_source=declared_source)
    assert with_source.error_code == EnumCoreErrorCode.TIMEOUT_EXCEEDED
    assert declared_source in str(with_source)
    assert "should already have been closed out by the runtime" in str(with_source)

    # A caller-chosen budget keeps the old, correct reading.
    caller_chosen = _poll()
    assert "has NOT failed" in str(caller_chosen)
    assert declared_source not in str(caller_chosen)
