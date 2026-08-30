# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Transport-level contract for the tenant delegation client (OMN-16967).

The seam under test is ``httpx.MockTransport`` — the real ``TransportCloudDelegation``
runs, builds real headers, and parses real response bodies; only the socket is
replaced. Patching the client's own methods would test the test.

Every failure class the beta guide's customers can actually hit is asserted by
name, because the whole point of this client is that a refusal reads as the
refusal it is: a rejected key, a fenced workflow type, a quota denial, and a
terminal ``failed`` run with no content are four different things.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from omnibase_core.enums.enum_core_error_code import EnumCoreErrorCode
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


def _ack_body(workflow_id: str = _WORKFLOW_ID) -> dict[str, object]:
    return {
        "workflow_id": workflow_id,
        "envelope_id": str(uuid.uuid4()),
        "correlation_id": str(uuid.uuid4()),
        "workflow_type": CLOUD_DELEGATION_WORKFLOW_TYPE,
        "status": "published",
    }


def _status_body(status: str, workflow_id: str = _WORKFLOW_ID) -> dict[str, object]:
    return {
        "workflow_id": workflow_id,
        "workflow_type": CLOUD_DELEGATION_WORKFLOW_TYPE,
        "status": status,
        "envelope_id": str(uuid.uuid4()),
        "correlation_id": str(uuid.uuid4()),
        "command_topic": "onex.cmd.delegation.inference.v1",
        "submitted_at": "2026-08-29T15:00:00Z",
        "updated_at": "2026-08-29T15:00:08Z",
    }


def _receipt_body(
    *, result_content: str | None = "a summary", status: str = "completed"
) -> dict[str, object]:
    return {
        "workflow_id": _WORKFLOW_ID,
        "tenant_id": str(uuid.uuid4()),
        "correlation_id": str(uuid.uuid4()),
        "workflow_type": CLOUD_DELEGATION_WORKFLOW_TYPE,
        "status": status,
        "submitted_at": "2026-08-29T15:00:00Z",
        "completed_at": "2026-08-29T15:00:08Z",
        "terminal_model_used": "gemini-2.5-flash-lite",
        "terminal_total_tokens": 99,
        "terminal_latency_ms": 1083,
        "result_content": result_content,
        "event_count": 4,
        "projection_row_hash": "31266a6d",
        "terminal_event_hash": "9fe84da7",
        "verifier": "my-laptop",
    }


def _client(handler: object) -> TransportCloudDelegation:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return TransportCloudDelegation(
        base_url=_BASE_URL,
        api_key=_KEY,
        http_client=httpx.Client(transport=transport),
    )


def test_submit_sends_the_api_key_header_and_the_fenced_free_workflow_type() -> None:
    """The submit is exactly the shape live-proven on 2026-08-29.

    Asserted on the wire, not on a constant: ``x-api-key`` and no
    ``authorization`` header (this path never carries a bearer), the pinned
    ``delegation-inference`` type, and ``max_tokens`` OMITTED when unset so the
    runtime resolves the budget from its own routing contract.
    """
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["api_key"] = request.headers.get("x-api-key")
        seen["authorization"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json=_ack_body())

    ack = _client(handler).submit(
        prompt="Summarize this.", task_type="summarization", max_tokens=None
    )

    assert seen["url"] == f"{_BASE_URL}/v1/workflows"
    assert seen["api_key"] == "onxk_testkey"
    assert seen["authorization"] is None
    assert seen["body"] == {
        "workflow_type": "delegation-inference",
        "payload": {"prompt": "Summarize this.", "task_type": "summarization"},
    }
    assert str(ack.workflow_id) == _WORKFLOW_ID


def test_submit_includes_max_tokens_only_when_the_caller_set_it() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(202, json=_ack_body())

    _client(handler).submit(prompt="p", task_type="summarization", max_tokens=512)

    payload = bodies[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["max_tokens"] == 512


def test_a_401_is_named_as_a_rejected_key_and_never_echoes_the_key() -> None:
    """A rejected dashboard key is an authentication error, not a generic 4xx.

    The message must also not contain the credential: this text lands in
    terminals, CI logs and support threads.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid api key"})

    with pytest.raises(ModelOnexError) as excinfo:
        _client(handler).submit(prompt="p", task_type="summarization", max_tokens=None)

    assert excinfo.value.error_code == EnumCoreErrorCode.AUTHENTICATION_ERROR
    message = str(excinfo.value)
    assert "401" in message
    assert "onxk_testkey" not in message


def test_a_fenced_workflow_type_is_distinguished_from_a_bad_request() -> None:
    """``fenced: true`` means "real type, deliberately not servable".

    That is an operator state and calls for a different customer action than a
    malformed request, so it must not collapse into the generic 400 branch.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "detail": "workflow type is fenced",
                "fenced": True,
            },
        )

    with pytest.raises(ModelOnexError) as excinfo:
        _client(handler).submit(prompt="p", task_type="summarization", max_tokens=None)

    assert excinfo.value.error_code == EnumCoreErrorCode.UNSUPPORTED_OPERATION
    assert "FENCED" in str(excinfo.value)


def test_a_plain_400_stays_a_plain_refusal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "unknown field 'temperature'"})

    with pytest.raises(ModelOnexError) as excinfo:
        _client(handler).submit(prompt="p", task_type="summarization", max_tokens=None)

    assert excinfo.value.error_code == EnumCoreErrorCode.INVALID_INPUT
    assert "unknown field" in str(excinfo.value)


def test_a_429_is_surfaced_immediately_and_never_retried() -> None:
    """A quota refusal retried becomes a timeout, which reads as a broken platform."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, json={"detail": "plan quota exhausted"})

    with pytest.raises(ModelOnexError) as excinfo:
        _client(handler).submit(prompt="p", task_type="summarization", max_tokens=None)

    assert excinfo.value.error_code == EnumCoreErrorCode.QUOTA_EXCEEDED
    assert len(calls) == 1


def test_an_unreachable_gateway_is_not_reported_as_a_refusal() -> None:
    """ "Refused you" and "is not there" have nothing in common operationally."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    with pytest.raises(ModelOnexError) as excinfo:
        _client(handler).submit(prompt="p", task_type="summarization", max_tokens=None)

    assert excinfo.value.error_code == EnumCoreErrorCode.NETWORK_ERROR
    assert "not a credential problem" in str(excinfo.value)


def test_poll_stops_on_a_terminal_failed_status_rather_than_waiting_it_out() -> None:
    """``failed`` is an answer. Continuing to poll it manufactures a timeout."""
    statuses = iter(["published", "failed"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_status_body(next(statuses)))

    result = _client(handler).poll_until_terminal(
        _WORKFLOW_ID, attempts=5, interval_seconds=0.0, sleep_fn=lambda _s: None
    )

    assert result.status == "failed"


def test_poll_raises_a_timeout_that_names_how_to_retrieve_the_run_later() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_status_body("published"))

    with pytest.raises(ModelOnexError) as excinfo:
        _client(handler).poll_until_terminal(
            _WORKFLOW_ID, attempts=2, interval_seconds=0.0, sleep_fn=lambda _s: None
        )

    assert excinfo.value.error_code == EnumCoreErrorCode.TIMEOUT_EXCEEDED
    message = str(excinfo.value)
    assert "has NOT failed" in message
    assert _WORKFLOW_ID in message


def test_receipt_passes_runner_identity_as_a_query_parameter() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_receipt_body())

    receipt = _client(handler).receipt(_WORKFLOW_ID, runner_identity="my-laptop")

    assert f"/v1/workflows/{_WORKFLOW_ID}/receipt" in str(seen["url"])
    assert "runner_identity=my-laptop" in str(seen["url"])
    assert receipt.result_content == "a summary"
    assert receipt.terminal_model_used == "gemini-2.5-flash-lite"


def test_a_receipt_with_null_result_content_parses_as_absent_not_empty() -> None:
    """The quota-dead shape: terminal, accepted, and carrying no content.

    ``None`` and ``""`` must stay distinguishable, because the CLI reports "the
    runtime returned no content" as a failure class of its own.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_receipt_body(result_content=None, status="failed")
        )

    receipt = _client(handler).receipt(_WORKFLOW_ID, runner_identity="ci")

    assert receipt.result_content is None
    assert receipt.status == "failed"


def test_an_additive_server_field_does_not_break_an_installed_client() -> None:
    """Response models are ``extra="ignore"`` on purpose.

    A customer's installed CLI is upgraded on their schedule; ``forbid`` here
    would turn the next additive server field into a client-side outage.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        body = _receipt_body()
        body["a_field_added_next_quarter"] = "value"
        return httpx.Response(200, json=body)

    receipt = _client(handler).receipt(_WORKFLOW_ID, runner_identity="ci")

    assert receipt.result_content == "a summary"
