# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18294 — the client reads the ceiling, and repeats what the gateway said.

Dogfood round 2 (2026-09-13) submitted a real 58,368-character standup prompt
through ``onex cloud delegate`` and got back::

    [ONEX_CORE_007_INVALID_INPUT] the gateway refused the request to submit
    the delegation with 400. Gateway detail: payload does not conform to the
    workflow contract

Two separate client-side defects are behind that sentence, and both are fixed
here:

1. **The client had no way to know the limit.** It spent a network round trip
   to be told it had broken a rule it could not read. The gateway now
   advertises its payload schemas at ``GET /v1/workflows/contracts``, so the
   client reads the ceiling and refuses locally — naming the limit AND the
   measured size, which is the pair that makes the refusal actionable.

2. **The client threw the gateway's answer away.** The gateway's 400 has
   always carried field-level ``errors`` — ``{"field": "prompt", "message":
   "must be at most 65536 characters"}`` — merged into the body next to
   ``detail``. ``_detail()`` read ``detail`` only. The generic sentence the
   customer saw was not the gateway being unhelpful; it was this client
   discarding the helpful part.

The second fix matters more than the first, because it is what makes EVERY
future contract violation legible, not just the one size rule this ticket
happened to find.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from omnibase_core.errors.model_onex_error import ModelOnexError
from pydantic import SecretStr

from omnimarket.cloud.transport_cloud_delegation import (
    CLOUD_DELEGATION_WORKFLOW_TYPE,
    TransportCloudDelegation,
)

pytestmark = pytest.mark.unit

_BASE_URL = "https://dev.api.omninode.ai"
_KEY = SecretStr("onxk_testkey")
_WORKFLOW_ID = "88ceab3f-37b3-4125-bc67-4c46a19eee5b"

# The ceiling the gateway declares for ``prompt`` (omninode_infra
# docker/onex-api/workflow-contracts.yaml). Written here as the number the
# FAKE gateway advertises, never as a client-side constant the real code
# reads — the whole point is that the client learns it over the wire.
_ADVERTISED_LIMIT = 65_536

# The measured dogfood input: 22 ledger rows rendered to a standup prompt.
_DOGFOOD_PROMPT_CHARS = 58_368


def _ack_body() -> dict[str, object]:
    return {
        "workflow_id": _WORKFLOW_ID,
        "envelope_id": str(uuid.uuid4()),
        "correlation_id": str(uuid.uuid4()),
        "workflow_type": CLOUD_DELEGATION_WORKFLOW_TYPE,
        "status": "published",
    }


def _contracts_body(prompt_max_length: int = _ADVERTISED_LIMIT) -> dict[str, object]:
    """The discovery payload, shaped exactly as the gateway renders it."""
    return {
        "contracts": [
            {
                "workflow_type": CLOUD_DELEGATION_WORKFLOW_TYPE,
                "contract_id": "node_delegation_orchestrator:0.6.0",
                "payload_schema": {
                    "type": "object",
                    "required": ["prompt", "task_type"],
                    "additionalProperties": False,
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": prompt_max_length,
                        },
                        "task_type": {"type": "string"},
                    },
                },
            }
        ]
    }


def _client(handler: object) -> TransportCloudDelegation:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return TransportCloudDelegation(
        base_url=_BASE_URL,
        api_key=_KEY,
        http_client=httpx.Client(transport=transport),
    )


def _gateway(
    *,
    prompt_max_length: int = _ADVERTISED_LIMIT,
    discovery_status: int = 200,
    seen: list[httpx.Request] | None = None,
):
    """A fake gateway that answers discovery and accepts any submission."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.path == "/v1/workflows/contracts":
            if discovery_status != 200:
                return httpx.Response(discovery_status, json={"detail": "Not Found"})
            return httpx.Response(200, json=_contracts_body(prompt_max_length))
        return httpx.Response(202, json=_ack_body())

    return handler


class TestLocalRefusalBeforeAnyNetworkCall:
    """AC1 + AC3. The refusal happens here, and it says what it measured."""

    def test_oversize_prompt_is_refused_without_submitting(self) -> None:
        """RED before OMN-18294: the client submitted and let the gateway refuse."""
        seen: list[httpx.Request] = []
        client = _client(_gateway(seen=seen))

        with pytest.raises(ModelOnexError) as excinfo:
            client.submit(
                prompt="x" * (_ADVERTISED_LIMIT + 1),
                task_type="summarization",
                max_tokens=None,
            )

        posted = [r for r in seen if r.method == "POST"]
        assert posted == [], "the oversize prompt must never reach the network"
        message = str(excinfo.value)
        assert str(_ADVERTISED_LIMIT) in message, message
        assert str(_ADVERTISED_LIMIT + 1) in message, message
        assert "prompt" in message, message

    def test_the_measured_dogfood_standup_now_submits(self) -> None:
        """The task that started this ticket goes through against the new ceiling."""
        seen: list[httpx.Request] = []
        ack = _client(_gateway(seen=seen)).submit(
            prompt="x" * _DOGFOOD_PROMPT_CHARS,
            task_type="summarization",
            max_tokens=None,
        )
        assert str(ack.workflow_id) == _WORKFLOW_ID
        assert [r for r in seen if r.method == "POST"], "the submission must be sent"

    def test_a_prompt_exactly_at_the_ceiling_submits(self) -> None:
        """The advertised bound is inclusive; refusing at it would refuse legal work."""
        ack = _client(_gateway()).submit(
            prompt="x" * _ADVERTISED_LIMIT, task_type="summarization", max_tokens=None
        )
        assert str(ack.workflow_id) == _WORKFLOW_ID

    def test_the_client_reads_the_gateways_number_not_its_own(self) -> None:
        """A gateway advertising a different ceiling is obeyed, not overridden.

        This is the assertion that proves the limit is CONTRACT-resolved. If
        the client ever hardcodes 65536, this test keeps passing for the wrong
        reason only if the constant happens to match — so it deliberately uses
        a ceiling no source file contains.
        """
        with pytest.raises(ModelOnexError) as excinfo:
            _client(_gateway(prompt_max_length=1_024)).submit(
                prompt="x" * 2_048, task_type="summarization", max_tokens=None
            )
        assert "1024" in str(excinfo.value), str(excinfo.value)


class TestDiscoveryCost:
    """One extra request per transport, not one per submission."""

    def test_discovery_is_fetched_once_and_reused(self) -> None:
        seen: list[httpx.Request] = []
        client = _client(_gateway(seen=seen))
        for _ in range(3):
            client.submit(prompt="ok", task_type="summarization", max_tokens=None)
        discovery = [r for r in seen if r.url.path == "/v1/workflows/contracts"]
        assert len(discovery) == 1, [str(r.url) for r in discovery]

    def test_discovery_carries_the_api_key(self) -> None:
        seen: list[httpx.Request] = []
        _client(_gateway(seen=seen)).submit(
            prompt="ok", task_type="summarization", max_tokens=None
        )
        discovery = next(r for r in seen if r.url.path == "/v1/workflows/contracts")
        assert discovery.headers.get("x-api-key") == "onxk_testkey"


class TestUnknownLimitDefersToTheGateway:
    """No invented ceiling. The authority that knows decides."""

    def test_submission_proceeds_when_the_gateway_does_not_advertise(self) -> None:
        """A gateway too old to serve discovery must not be second-guessed.

        The client could pick a default here. It must not: a client-side
        number that the gateway never agreed to is exactly the undeclared,
        unreadable limit this ticket exists to remove — it would merely move
        it from the server to the laptop. When the ceiling is unknown the
        submission goes out and the gateway refuses it if it must, now with a
        refusal that names the field and the limit.
        """
        seen: list[httpx.Request] = []
        ack = _client(_gateway(discovery_status=404, seen=seen)).submit(
            prompt="x" * 1_000_000, task_type="summarization", max_tokens=None
        )
        assert str(ack.workflow_id) == _WORKFLOW_ID
        assert [r for r in seen if r.method == "POST"]

    def test_a_discovery_outage_does_not_fail_a_submission(self) -> None:
        """Discovery is an optimisation on legibility, never a new dependency."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/workflows/contracts":
                raise httpx.ConnectError("discovery unreachable")
            return httpx.Response(202, json=_ack_body())

        ack = _client(handler).submit(
            prompt="ok", task_type="summarization", max_tokens=None
        )
        assert str(ack.workflow_id) == _WORKFLOW_ID


class TestGatewayErrorsAreRepeated:
    """AC4, client half. The gateway named the field; say so."""

    def test_field_level_errors_appear_in_the_raised_message(self) -> None:
        """RED before OMN-18294: only ``detail`` was read; ``errors`` was dropped."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/workflows/contracts":
                return httpx.Response(404, json={"detail": "Not Found"})
            return httpx.Response(
                400,
                json={
                    "detail": "payload does not conform to the workflow contract",
                    "workflow_type": CLOUD_DELEGATION_WORKFLOW_TYPE,
                    "contract_id": "node_delegation_orchestrator:0.6.0",
                    "errors": [
                        {
                            "field": "prompt",
                            "message": "must be at most 65536 characters",
                        }
                    ],
                },
            )

        with pytest.raises(ModelOnexError) as excinfo:
            _client(handler).submit(
                prompt="x" * 10, task_type="summarization", max_tokens=None
            )

        message = str(excinfo.value)
        assert "payload does not conform to the workflow contract" in message
        assert "prompt" in message, message
        assert "must be at most 65536 characters" in message, message

    def test_multiple_field_errors_are_all_reported(self) -> None:
        """Fixing one violation and resubmitting to find the next is not a workflow."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/workflows/contracts":
                return httpx.Response(404, json={"detail": "Not Found"})
            return httpx.Response(
                400,
                json={
                    "detail": "payload does not conform to the workflow contract",
                    "errors": [
                        {
                            "field": "prompt",
                            "message": "must be at most 65536 characters",
                        },
                        {
                            "field": "task_type",
                            "message": "does not match pattern ^(test|summarization)$",
                        },
                    ],
                },
            )

        with pytest.raises(ModelOnexError) as excinfo:
            _client(handler).submit(prompt="x", task_type="nonsense", max_tokens=None)
        message = str(excinfo.value)
        assert "prompt" in message, message
        assert "task_type" in message, message

    def test_a_body_without_errors_still_reports_its_detail(self) -> None:
        """The added rendering must not swallow the plain case."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/workflows/contracts":
                return httpx.Response(404, json={"detail": "Not Found"})
            return httpx.Response(400, json={"detail": "workflow_type is unknown"})

        with pytest.raises(ModelOnexError) as excinfo:
            _client(handler).submit(
                prompt="x", task_type="summarization", max_tokens=None
            )
        assert "workflow_type is unknown" in str(excinfo.value)

    def test_a_malformed_errors_field_does_not_mask_the_detail(self) -> None:
        """A server sending something unexpected must not crash the client.

        The customer still needs to see ``detail``; an exception raised while
        rendering an error would replace a legible refusal with a traceback.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/workflows/contracts":
                return httpx.Response(404, json={"detail": "Not Found"})
            return httpx.Response(
                400, json={"detail": "refused", "errors": "not-a-list"}
            )

        with pytest.raises(ModelOnexError) as excinfo:
            _client(handler).submit(
                prompt="x", task_type="summarization", max_tokens=None
            )
        assert "refused" in str(excinfo.value)


def test_submission_wire_shape_is_unchanged() -> None:
    """The added discovery call must not alter what a submission looks like."""
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/workflows/contracts":
            return httpx.Response(200, json=_contracts_body())
        bodies.append(json.loads(request.content))
        return httpx.Response(202, json=_ack_body())

    _client(handler).submit(
        prompt="Summarize this.", task_type="summarization", max_tokens=None
    )
    assert bodies == [
        {
            "workflow_type": "delegation-inference",
            "payload": {"prompt": "Summarize this.", "task_type": "summarization"},
        }
    ]
