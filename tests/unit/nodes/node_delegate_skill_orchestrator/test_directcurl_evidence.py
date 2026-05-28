# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for DirectCurlDelegationDispatchPort evidence persistence."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_direct_curl_dispatch,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_direct_curl_dispatch import (
    DirectCurlDelegationDispatchPort,
)


@pytest.fixture
def fake_backends() -> list[dict[str, object]]:
    return [
        {
            "backend_id": "test-local",
            "endpoint_url": "http://127.0.0.1:8000",
            "model_name": "test-model",
            "tier": "local",
            "capabilities": ["code_generation"],
            "use_for": ["code_generation"],
        }
    ]


def _patch_curl(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_call_via_curl(
        *,
        endpoint_url: str,
        model: str,
        system_prompt: str,
        prompt: str,
        max_tokens: int,
    ) -> dict[str, object]:
        return {
            "content": "def reverse(s): return s[::-1]",
            "model_used": model,
            "latency_ms": 42,
            "prompt_tokens": 11,
            "completion_tokens": 22,
            "total_tokens": 33,
        }

    monkeypatch.setattr(
        port_direct_curl_dispatch,
        "_call_via_curl",
        fake_call_via_curl,
    )


def test_dispatch_persists_evidence_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_backends: list[dict[str, object]],
) -> None:
    """A successful dispatch writes one row to delegation_events."""
    db_path = tmp_path / "delegation.sqlite"
    _patch_curl(monkeypatch)

    port = DirectCurlDelegationDispatchPort(evidence_db_path=db_path)
    port._backends = fake_backends  # bypass YAML loading

    correlation_id = uuid4()
    result = asyncio.run(
        port.dispatch(
            prompt="reverse a string",
            task_type="code_generation",
            correlation_id=correlation_id,
            max_tokens=256,
            source_file_path=None,
            source_session_id="sess-test",
            wait=True,
            quality_contract_mode="lenient",
            acceptance_criteria=(),
        )
    )

    assert result["status"] == "completed"

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
    assert row["delegated_to"] == "http://127.0.0.1:8000"
    assert row["model_name"] == "test-model"
    assert row["quality_gate_passed"] == 1
    assert row["latency_ms"] == 42
    assert row["delegation_latency_ms"] == 42
    assert row["tokens_input"] == 11
    assert row["tokens_output"] == 22
    assert row["prompt_text"] == "reverse a string"
    assert row["response_text"] == "def reverse(s): return s[::-1]"
    assert row["session_id"] == "sess-test"
    assert row["created_at"] > 0


def test_dispatch_persists_evidence_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_backends: list[dict[str, object]],
) -> None:
    """Two dispatches with the same correlation_id UPSERT to one row."""
    db_path = tmp_path / "delegation.sqlite"
    _patch_curl(monkeypatch)

    port = DirectCurlDelegationDispatchPort(evidence_db_path=db_path)
    port._backends = fake_backends

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


def test_dispatch_evidence_failure_does_not_break_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_backends: list[dict[str, object]],
) -> None:
    """A SQLite failure is swallowed; the dispatch response still succeeds."""
    # Point the DB at a path that cannot be created (parent is a regular file).
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    db_path = blocker / "delegation.sqlite"

    _patch_curl(monkeypatch)

    port = DirectCurlDelegationDispatchPort(evidence_db_path=db_path)
    port._backends = fake_backends

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
