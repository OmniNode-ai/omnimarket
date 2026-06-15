# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Evidence-materialization tests for the in-process LocalDelegationDispatchPort.

OMN-13160: the standalone CLI delegation path composes the routing authority +
the canonical effect handler (curl on the macOS LAN profile, httpx elsewhere) +
the canonical projection (``HandlerProjectionDelegation``). These tests prove the
LOCAL in-process path STILL materializes a ``delegation_events`` row — the
deprecated DirectCurl port's bespoke sqlite write is replaced by the canonical
projection against a local SQLite target, and the local evidence is not dropped.

The transport is monkeypatched at the boundary so no network call is made — the
assertion is about routing -> effect -> projection composition and the
materialized evidence row, not endpoint reachability.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.routing import delegation_backend_resolution


@pytest.fixture
def fake_backends() -> list[dict[str, object]]:
    return [
        {
            "backend_id": "local-coder",
            # COMPLETE chat-completions URL (overlay-supplied), posted verbatim.
            "endpoint_url": "http://inference.example:8000/v1/chat/completions",
            "model_name": "Qwen3.6-35B-A3B",
            "tier": "local",
            # OMN-13161: per-backend output-token ceiling resolved by the router.
            "max_tokens": 65536,
            "capabilities": ["code_generation"],
        }
    ]


def _patch_routing(
    monkeypatch: pytest.MonkeyPatch, backends: list[dict[str, object]]
) -> None:
    """Resolve the routing authority off the in-memory fake backend list."""
    monkeypatch.setattr(
        delegation_backend_resolution,
        "load_bifrost_backends",
        lambda **_: backends,
    )


def _patch_transport(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch the effect handler transport to return a typed body (no network)."""
    captured: dict[str, Any] = {}

    def fake_probe_health(endpoint_url: str, **_: Any) -> bool:
        captured["probe_url"] = endpoint_url
        return True

    def fake_post(
        *,
        endpoint_url: str,
        payload: dict[str, Any],
        extra_headers: dict[str, str] | None = None,
        runtime_profile: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> transport.ModelTransportResponse:
        captured["post_url"] = endpoint_url
        captured["payload"] = payload
        return transport.ModelTransportResponse(
            status_code=200,
            json_body={
                "choices": [{"message": {"content": "def reverse(s): return s[::-1]"}}],
                "model": "Qwen3.6-35B-A3B",
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 22,
                    "total_tokens": 33,
                },
            },
            latency_ms=42,
        )

    monkeypatch.setattr(transport, "probe_health", fake_probe_health)
    monkeypatch.setattr(transport, "post_chat_completion", fake_post)
    return captured


def test_local_dispatch_materializes_evidence_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_backends: list[dict[str, object]],
) -> None:
    """A successful local dispatch writes one row to delegation_events."""
    db_path = tmp_path / "delegation.sqlite"
    _patch_routing(monkeypatch, fake_backends)
    _patch_transport(monkeypatch)

    port = LocalDelegationDispatchPort(evidence_db_path=db_path)
    correlation_id = uuid4()
    result = asyncio.run(
        port.dispatch(
            prompt="reverse a string",
            task_type="code_generation",
            correlation_id=correlation_id,
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            quality_contract_mode="lenient",
            acceptance_criteria=(),
        )
    )

    assert result["status"] == "completed"
    assert result["content"] == "def reverse(s): return s[::-1]"
    assert result["delegated_to"] == (
        "http://inference.example:8000/v1/chat/completions"
    )

    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM delegation_events WHERE correlation_id = ?",
            (str(correlation_id),),
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    row = rows[0]
    assert row["correlation_id"] == str(correlation_id)
    assert row["task_type"] == "code_generation"
    assert row["model_name"] == "Qwen3.6-35B-A3B"
    assert bool(row["quality_gate_passed"]) is True
    assert row["tokens_input"] == 11
    assert row["tokens_output"] == 22
    assert row["prompt_text"] == "reverse a string"
    assert "def reverse" in row["response_text"]
    assert row["delegation_latency_ms"] == 42


def test_local_dispatch_evidence_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_backends: list[dict[str, object]],
) -> None:
    """Two dispatches with the same correlation_id UPSERT to one row."""
    db_path = tmp_path / "delegation.sqlite"
    _patch_routing(monkeypatch, fake_backends)
    _patch_transport(monkeypatch)

    port = LocalDelegationDispatchPort(evidence_db_path=db_path)
    correlation_id = uuid4()
    for _ in range(2):
        asyncio.run(
            port.dispatch(
                prompt="reverse a string",
                task_type="code_generation",
                correlation_id=correlation_id,
                max_tokens=256,
                source_file_path=None,
                source_session_id=None,
                wait=True,
                quality_contract_mode="lenient",
                acceptance_criteria=(),
            )
        )

    conn = sqlite3.connect(str(db_path))
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM delegation_events WHERE correlation_id = ?",
            (str(correlation_id),),
        ).fetchone()[0]
    finally:
        conn.close()

    assert count == 1


def test_local_dispatch_evidence_failure_does_not_break_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_backends: list[dict[str, object]],
) -> None:
    """A projection failure is swallowed; the dispatch response still succeeds."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    db_path = blocker / "delegation.sqlite"

    _patch_routing(monkeypatch, fake_backends)
    _patch_transport(monkeypatch)

    port = LocalDelegationDispatchPort(evidence_db_path=db_path)
    result = asyncio.run(
        port.dispatch(
            prompt="reverse a string",
            task_type="code_generation",
            correlation_id=uuid4(),
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            quality_contract_mode="lenient",
            acceptance_criteria=(),
        )
    )

    assert result["status"] == "completed"
    assert result["content"] == "def reverse(s): return s[::-1]"


def test_local_dispatch_reaches_lan_endpoint_via_curl_on_macos_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_backends: list[dict[str, object]],
) -> None:
    """On the macOS profile the effect handler posts the LAN URL via curl, verbatim.

    Patches subprocess.run (the curl boundary) so no network call is made, and
    asserts the curl argv carries the resolved endpoint_url byte-for-byte — the
    LAN-safe transport (OMN-13160) carrying #1228's verbatim-URL behavior.
    """
    import json
    import subprocess

    db_path = tmp_path / "delegation.sqlite"
    monkeypatch.setenv("RUNTIME_PROFILE", "local_macos_claude_hooks")
    _patch_routing(monkeypatch, fake_backends)

    captured: dict[str, Any] = {}

    class _FakeProc:
        returncode = 0
        stderr = ""
        stdout = json.dumps(
            {
                "choices": [{"message": {"content": "ok"}}],
                "model": "Qwen3.6-35B-A3B",
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        )

    def fake_run(args: list[str], **_: Any) -> _FakeProc:
        # First call is the health probe (curl ... /health); the POST follows.
        if "-X" in args and "POST" in args:
            captured["post_args"] = args
        return _FakeProc()

    monkeypatch.setattr(subprocess, "run", fake_run)

    port = LocalDelegationDispatchPort(evidence_db_path=db_path)
    result = asyncio.run(
        port.dispatch(
            prompt="reverse a string",
            task_type="code_generation",
            correlation_id=uuid4(),
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            quality_contract_mode="lenient",
            acceptance_criteria=(),
        )
    )

    assert result["status"] == "completed"
    post_args = captured["post_args"]
    endpoint = "http://inference.example:8000/v1/chat/completions"
    # The curl argv carries the resolved endpoint_url verbatim, exactly once,
    # as the POST target (the element after it is -d).
    assert endpoint in post_args
    url_index = post_args.index(endpoint)
    assert post_args[url_index + 1] == "-d"
    url_like = [a for a in post_args if a.startswith(("http://", "https://"))]
    assert url_like == [endpoint]
    # Defense in depth against the OMN-13159 doubled-chat-path regression.
    assert "/v1/chat/completions/v1/chat/completions" not in " ".join(post_args)


def test_local_dispatch_unset_max_tokens_uses_backend_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_backends: list[dict[str, object]],
) -> None:
    """OMN-13161: an unset request max_tokens resolves to the backend ceiling.

    The fake backend declares max_tokens=65536; with the request carrying None the
    outbound effect-handler payload must carry 65536.
    """
    db_path = tmp_path / "delegation.sqlite"
    _patch_routing(monkeypatch, fake_backends)
    captured = _patch_transport(monkeypatch)

    port = LocalDelegationDispatchPort(evidence_db_path=db_path)
    result = asyncio.run(
        port.dispatch(
            prompt="reverse a string",
            task_type="code_generation",
            correlation_id=uuid4(),
            max_tokens=None,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            quality_contract_mode="lenient",
            acceptance_criteria=(),
        )
    )

    assert result["status"] == "completed"
    assert captured["payload"]["max_tokens"] == 65536


def test_local_dispatch_explicit_max_tokens_capped_at_backend_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_backends: list[dict[str, object]],
) -> None:
    """OMN-13161: an explicit request above the backend ceiling is capped to it.

    The backend ceiling is 65536; a request for 200000 must be clamped to 65536.
    """
    db_path = tmp_path / "delegation.sqlite"
    _patch_routing(monkeypatch, fake_backends)
    captured = _patch_transport(monkeypatch)

    port = LocalDelegationDispatchPort(evidence_db_path=db_path)
    result = asyncio.run(
        port.dispatch(
            prompt="reverse a string",
            task_type="code_generation",
            correlation_id=uuid4(),
            max_tokens=200000,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            quality_contract_mode="lenient",
            acceptance_criteria=(),
        )
    )

    assert result["status"] == "completed"
    assert captured["payload"]["max_tokens"] == 65536


def test_local_dispatch_explicit_max_tokens_below_ceiling_passes_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_backends: list[dict[str, object]],
) -> None:
    """OMN-13161: an explicit request below the backend ceiling is used verbatim."""
    db_path = tmp_path / "delegation.sqlite"
    _patch_routing(monkeypatch, fake_backends)
    captured = _patch_transport(monkeypatch)

    port = LocalDelegationDispatchPort(evidence_db_path=db_path)
    result = asyncio.run(
        port.dispatch(
            prompt="reverse a string",
            task_type="code_generation",
            correlation_id=uuid4(),
            max_tokens=4096,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            quality_contract_mode="lenient",
            acceptance_criteria=(),
        )
    )

    assert result["status"] == "completed"
    assert captured["payload"]["max_tokens"] == 4096
