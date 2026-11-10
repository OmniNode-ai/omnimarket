# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real local credential/identity, native HTTP, artifact and projection composition."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from omnimarket.handlers import claude_gateway as composition
from omnimarket.handlers.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.local_deployment.tenant_identity import mint_local_tenant_identity
from omnimarket.models.delegation.model_claude_gateway import ModelClaudeGatewayConfig

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


def artifact_bytes(root: Path) -> list[bytes]:
    return [
        path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.stat().st_size <= 1024 * 1024
    ]


@pytest.mark.parametrize("failure", ["none", "provider", "projection"])
async def test_gateway_joins_actual_receipt_and_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    store = LocalByokCredentialStore(db_path=tmp_path / "delegation.sqlite")
    await store.set_secret("gateway.fixture", "gateway-SENTINEL")
    await store.set_secret("upstream.fixture", "upstream-SENTINEL")
    identity = mint_local_tenant_identity(db_path=store.db_path)
    backend = ModelResolvedDelegationBackend(
        backend_id="fixture-native",
        model_id="claude-fixture",
        endpoint_ref="https://upstream.example/v1/messages",
        max_tokens=4096,
        timeout_ms=30000,
        secret_ref="upstream.fixture",
    )
    resolved: list[str] = []

    def resolve(
        task_type: str, *, backend_id: str, store: object
    ) -> ModelResolvedDelegationBackend:
        resolved.append(backend_id)
        assert task_type == "complex_reasoning"
        return backend

    monkeypatch.setattr(composition, "resolve_delegation_backend", resolve)
    if failure == "projection":

        def refuse(*args: object, **kwargs: object) -> None:
            raise RuntimeError("projection-SENTINEL")

        monkeypatch.setattr(composition.HandlerProjectionDelegation, "project", refuse)
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["x-api-key"] == "upstream-SENTINEL"
        if failure == "provider":
            return httpx.Response(503, json={"error": "upstream-SENTINEL"})
        return httpx.Response(
            200,
            json={
                "id": "msg_fixture",
                "type": "message",
                "model": "claude-fixture",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "fixture-answer"}],
                "usage": {"input_tokens": 3, "output_tokens": 5},
            },
        )

    root = tmp_path / ".onex_state"
    app = composition.build_local_gateway_application(
        ModelClaudeGatewayConfig(
            backend_id="fixture-native",
            model="claude-fixture",
            credential_ref="gateway.fixture",
        ),
        state_root=root,
        store=store,
        transport=httpx.MockTransport(respond),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        response = await client.post(
            "/v1/messages",
            json={
                "model": "claude-fixture",
                "max_tokens": 50,
                "messages": [{"role": "user", "content": "fixture-prompt"}],
            },
            headers={"authorization": "Bearer gateway-SENTINEL"},
        )
    assert response.status_code == (200 if failure == "none" else 502)
    assert resolved
    assert len(seen) == 1
    assert "SENTINEL" not in response.text
    run_id = response.headers["x-onex-run-id"]
    blobs = artifact_bytes(root)
    receipts = [
        json.loads(blob)
        for blob in blobs
        if b'"prompt_hash"' in blob and run_id.encode() in blob
    ]
    assert receipts
    assert all(b"SENTINEL" not in blob for blob in blobs)
    assert all(
        b"fixture-prompt" not in blob and b"fixture-answer" not in blob
        for blob in blobs
    )
    if failure != "projection":
        with sqlite3.connect(store.db_path.as_uri() + "?mode=ro", uri=True) as database:
            database.row_factory = sqlite3.Row
            row = database.execute(
                "SELECT * FROM delegation_events WHERE correlation_id = ?", (run_id,)
            ).fetchone()
        assert row is not None
        assert row["tenant_id"] == str(identity.tenant_uuid)
        assert row["model_name"] == "claude-fixture"
        assert row["terminal_ok"] == (failure == "none")
        assert row["cost_usd"] is None
        assert not row["prompt_text"]
        assert not row["response_text"]
        assert not row["quality_gate_passed"]
        if failure == "none":
            assert (row["tokens_input"], row["tokens_output"]) == (3, 5)
            assert receipts[0]["status"] == "completed"
    positive = tmp_path / "positive-control"
    positive.mkdir()
    (positive / "planted.txt").write_text("gateway-SENTINEL")
    assert any(b"SENTINEL" in blob for blob in artifact_bytes(positive))


async def test_missing_identity_refuses_before_gateway_start(tmp_path: Path) -> None:
    store = LocalByokCredentialStore(db_path=tmp_path / "delegation.sqlite")
    with pytest.raises(ValueError, match="onex local init"):
        composition.build_local_gateway_application(
            ModelClaudeGatewayConfig(
                backend_id="fixture-native",
                model="claude-fixture",
                credential_ref="gateway.fixture",
            ),
            state_root=tmp_path / "state",
            store=store,
        )
