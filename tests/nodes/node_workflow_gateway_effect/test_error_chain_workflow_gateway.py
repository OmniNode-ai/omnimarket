# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: every refusal of the mounted workflow gateway is typed and publishes nothing.

Exemplar: tests/nodes/node_plan_list/test_http_plans.py (HTTP error cases through the mount).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest

from omnimarket.nodes.node_workflow_gateway_effect.handlers.adapters_memory import (
    InMemoryWorkflowPublisher,
    InMemoryWorkflowStore,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.http_app import create_app
from tests.nodes.node_workflow_gateway_effect.fakes import (
    API_KEY_HEADER,
    KEY_A,
    KEY_B,
    NOW,
    SKILL_PAYLOAD,
    Gateway,
    build_gateway,
)

pytestmark = pytest.mark.unit
SUBMISSION = {"workflow_type": "delegate-skill", "payload": SKILL_PAYLOAD}
AUTH_A = {API_KEY_HEADER: KEY_A}


def _client(gateway: Gateway) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(dependencies=gateway.deps))
    return httpx.AsyncClient(transport=transport, base_url="http://runtime")


def _store(
    *,
    monthly_limit: int | None = None,
    hard_cap: bool = False,
    fail_with: Exception | None = None,
) -> InMemoryWorkflowStore:
    return InMemoryWorkflowStore(
        monthly_limit=monthly_limit,
        hard_cap=hard_cap,
        clock=lambda: NOW,
        fail_with=fail_with,
    )


async def test_no_credential_is_401_absent_and_a_bad_key_is_401_invalid() -> None:
    gateway = build_gateway()
    async with _client(gateway) as client:
        none = await client.post("/v1/workflows", json=SUBMISSION)
        bad = await client.post(
            "/v1/workflows", json=SUBMISSION, headers={API_KEY_HEADER: "onxk_nope"}
        )
    assert none.status_code == 401
    assert none.json() == {"detail": "Unauthorized", "code": "auth.credential.absent"}
    assert bad.status_code == 401
    assert bad.json()["code"] == "auth.credential.invalid"
    assert gateway.publisher.published == []


async def test_a_bearer_header_never_falls_through_to_an_api_key() -> None:
    gateway = build_gateway()  # no bearer resolver bound
    async with _client(gateway) as client:
        response = await client.post(
            "/v1/workflows",
            json=SUBMISSION,
            headers={"authorization": "Bearer abc", **AUTH_A},
        )
    assert response.status_code == 401
    assert response.json()["code"] == "auth.credential.invalid"
    assert gateway.publisher.published == []


REFUSALS = [
    pytest.param(b"{not json", 400, "request body must be valid JSON", id="not-json"),
    pytest.param(
        b'{"workflow_type": "delegate-skill", "payload": {}, "command_topic": "x"}',
        400,
        "invalid workflow submission",
        id="caller-supplied-topic",
    ),
    pytest.param(
        b'{"workflow_type": "nope", "payload": {}}',
        400,
        "unknown workflow_type",
        id="unknown-type",
    ),
    pytest.param(
        b'{"workflow_type": "push-validation", "payload": {}}',
        400,
        "FENCED",
        id="fenced-type",
    ),
    pytest.param(
        b'{"workflow_type": "delegate-skill", "payload": {"prompt": "x"}}',
        400,
        "does not conform",
        id="payload-schema",
    ),
    pytest.param(b"x" * 70000, 413, "too large", id="body-too-large"),
]


@pytest.mark.parametrize(("content", "status", "needle"), REFUSALS)
async def test_refused_submissions_publish_nothing(
    content: bytes, status: int, needle: str
) -> None:
    gateway = build_gateway()
    async with _client(gateway) as client:
        response = await client.post("/v1/workflows", content=content, headers=AUTH_A)
    assert response.status_code == status
    assert needle in response.json()["detail"]
    assert gateway.publisher.published == []


async def test_unknown_and_fenced_types_list_only_servable_types() -> None:
    gateway = build_gateway()
    async with _client(gateway) as client:
        fenced = await client.post(
            "/v1/workflows",
            json={"workflow_type": "push-validation", "payload": {}},
            headers=AUTH_A,
        )
    body = fenced.json()
    assert body["fenced"] is True
    assert body["allowed_workflow_types"] == ["delegate-skill", "delegation-inference"]


async def test_rate_limit_is_429_with_a_recorded_event() -> None:
    gateway = build_gateway(refill_per_minute=1, burst_capacity=1)
    async with _client(gateway) as client:
        first = await client.post("/v1/workflows", json=SUBMISSION, headers=AUTH_A)
        second = await client.post("/v1/workflows", json=SUBMISSION, headers=AUTH_A)
    assert first.status_code == 202
    assert second.status_code == 429
    assert int(second.headers["retry-after"]) >= 1
    body = second.json()
    assert {"limit_per_minute", "burst_capacity", "retry_after_seconds"} <= body.keys()
    assert body["exceeded_event_id"] == str(gateway.store.rate_limit_events[0])
    assert len(gateway.publisher.published) == 1


async def test_hard_monthly_quota_is_429_and_stores_no_second_row() -> None:
    store = _store(monthly_limit=1, hard_cap=True)
    gateway = build_gateway(store=store)
    async with _client(gateway) as client:
        first = await client.post("/v1/workflows", json=SUBMISSION, headers=AUTH_A)
        second = await client.post("/v1/workflows", json=SUBMISSION, headers=AUTH_A)
    assert first.status_code == 202
    assert second.status_code == 429
    body = second.json()
    assert body["detail"] == "monthly workflow quota exceeded"
    assert (body["used"], body["limit_value"], body["remaining"]) == (1, 1, 0)
    assert len(gateway.publisher.published) == 1


async def test_soft_monthly_quota_still_admits() -> None:
    gateway = build_gateway(store=_store(monthly_limit=1, hard_cap=False))
    async with _client(gateway) as client:
        statuses = [
            (
                await client.post("/v1/workflows", json=SUBMISSION, headers=AUTH_A)
            ).status_code
            for _ in range(2)
        ]
    assert statuses == [202, 202]


async def test_publish_failure_is_503_and_marks_the_row_failed_publish() -> None:
    gateway = build_gateway(
        publisher=InMemoryWorkflowPublisher(fail_with=RuntimeError("x"))
    )
    async with _client(gateway) as client:
        response = await client.post("/v1/workflows", json=SUBMISSION, headers=AUTH_A)
    assert response.status_code == 503
    body = response.json()
    assert "could not be published" in body["detail"]
    row = await gateway.store.fetch(
        workflow_id=UUID(body["workflow_id"]),
        tenant_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
    )
    assert row is not None
    assert row.status == "failed_publish"
    assert gateway.publisher.published == []


async def test_store_down_is_503_on_submit_and_on_status() -> None:
    gateway = build_gateway(store=_store(fail_with=RuntimeError("db")))
    async with _client(gateway) as client:
        submit = await client.post("/v1/workflows", json=SUBMISSION, headers=AUTH_A)
        status = await client.get(f"/v1/workflows/{uuid4()}/status", headers=AUTH_A)
    assert submit.status_code == 503
    assert submit.json()["detail"] == "workflow submission could not be recorded"
    assert status.status_code == 503
    assert status.json()["detail"] == "workflow status is unavailable"
    assert gateway.publisher.published == []


async def test_another_tenants_workflow_is_the_same_404_as_an_unknown_id() -> None:
    gateway = build_gateway()
    async with _client(gateway) as client:
        ack = (
            await client.post("/v1/workflows", json=SUBMISSION, headers=AUTH_A)
        ).json()
        other = await client.get(
            f"/v1/workflows/{ack['workflow_id']}/status",
            headers={API_KEY_HEADER: KEY_B},
        )
        unknown = await client.get(
            f"/v1/workflows/{uuid4()}/status", headers={API_KEY_HEADER: KEY_B}
        )
        other_receipt = await client.get(
            f"/v1/workflows/{ack['workflow_id']}/receipt",
            params={"runner_identity": "r"},
            headers={API_KEY_HEADER: KEY_B},
        )
    assert other.status_code == unknown.status_code == other_receipt.status_code == 404
    assert other.content == unknown.content == other_receipt.content
    assert other.json() == {
        "detail": "workflow not found",
        "code": "resource.not_found",
    }


async def test_status_and_receipt_input_errors() -> None:
    gateway = build_gateway()
    async with _client(gateway) as client:
        ack = (
            await client.post("/v1/workflows", json=SUBMISSION, headers=AUTH_A)
        ).json()
        wid = ack["workflow_id"]
        bad_id = await client.get("/v1/workflows/not-a-uuid/status", headers=AUTH_A)
        no_runner = await client.get(f"/v1/workflows/{wid}/receipt", headers=AUTH_A)
        blank_runner = await client.get(
            f"/v1/workflows/{wid}/receipt",
            params={"runner_identity": " "},
            headers=AUTH_A,
        )
        not_terminal = await client.get(
            f"/v1/workflows/{wid}/receipt",
            params={"runner_identity": "r"},
            headers=AUTH_A,
        )
        no_auth = await client.get(f"/v1/workflows/{wid}/status")
    assert bad_id.status_code == 400
    assert no_runner.status_code == 422
    assert blank_runner.status_code == 400
    assert not_terminal.status_code == 409
    assert no_auth.status_code == 401


async def test_no_overlay_document_answers_503_on_every_route(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://runtime"
    ) as client:
        answers = [
            await client.post("/v1/workflows", json=SUBMISSION, headers=AUTH_A),
            await client.get("/v1/workflows/contracts", headers=AUTH_A),
            await client.get(f"/v1/workflows/{uuid4()}/status", headers=AUTH_A),
            await client.get(
                f"/v1/workflows/{uuid4()}/receipt",
                params={"runner_identity": "r"},
                headers=AUTH_A,
            ),
        ]
    assert [a.status_code for a in answers] == [503, 503, 503, 503]
    assert {a.json()["detail"] for a in answers} == {
        "workflow gateway is not configured"
    }
