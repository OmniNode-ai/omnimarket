# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_delegate_skill_orchestrator (OMN-12704).

Exercises the delegation route end-to-end through the node handler with a stub
dispatch port (no network): a typed ModelDelegateSkillRequest flows through
HandlerDelegateSkill and yields a typed ModelDelegateSkillResponse that preserves
the route's quality-gate result, model/backend selection, correlation id, and
output content. This is the authority that node_task_execution_orchestrator
composes for coding/refactor/review work; the parity is asserted here so the
delegation route stays the single owner of delegation success/failure.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.routing import delegation_backend_resolution


class _StubDispatchPort:
    """In-process delegation dispatch port returning typed evidence (no network)."""

    def __init__(self, result: dict[str, object]) -> None:
        self._result = result
        self.calls: list[dict[str, object]] = []

    async def dispatch(
        self,
        *,
        prompt: str,
        task_type: str,
        correlation_id: UUID,
        max_tokens: int | None,
        source_file_path: str | None,
        source_session_id: str | None,
        wait: bool,
        quality_contract_mode: str,
        acceptance_criteria: tuple[str, ...],
    ) -> dict[str, object]:
        self.calls.append(
            {
                "prompt": prompt,
                "task_type": task_type,
                "correlation_id": correlation_id,
            }
        )
        return self._result


@pytest.mark.unit
class TestDelegateSkillGoldenChain:
    """Request -> HandlerDelegateSkill -> typed response chain (stubbed port)."""

    async def test_completed_delegation_preserves_route_evidence(self) -> None:
        correlation_id = uuid4()
        port = _StubDispatchPort(
            {
                "status": "completed",
                "content": "def parse(): ...",
                "delegated_to": "local-runtime",
                "model_name": "qwen-coder",
                "quality_gate_passed": True,
                "quality_score": 0.91,
            }
        )
        handler = HandlerDelegateSkill(dispatch_port=port)

        response = await handler.handle(
            ModelDelegateSkillRequest(
                prompt="generate a parser for the config file",
                task_type="code_generation",
                source="claude-code",
                correlation_id=correlation_id,
            )
        )

        assert response.status == "completed"
        assert response.correlation_id == correlation_id
        assert response.task_type == "code_generation"
        assert response.provider == "local-runtime"
        assert response.model_name == "qwen-coder"
        assert response.prompt_text == "generate a parser for the config file"
        assert response.response == "def parse(): ..."
        assert response.quality_gate_passed is True
        # The route dispatched exactly the requested coding work.
        assert port.calls[0]["task_type"] == "code_generation"
        assert port.calls[0]["prompt"] == "generate a parser for the config file"

    async def test_failed_dispatch_stays_typed_failure(self) -> None:
        """A dispatch exception is surfaced as a typed failed response by the route."""

        class _RaisingPort:
            async def dispatch(self, **_: object) -> dict[str, object]:
                raise RuntimeError("backend unavailable")

        handler = HandlerDelegateSkill(dispatch_port=_RaisingPort())
        correlation_id = uuid4()

        response = await handler.handle(
            ModelDelegateSkillRequest(
                prompt="do work",
                task_type="code_generation",
                source="claude-code",
                correlation_id=correlation_id,
            )
        )

        assert response.status == "failed"
        assert response.correlation_id == correlation_id
        assert "backend unavailable" in response.error_message

    async def test_unit_test_prompt_suppresses_reasoning_and_persists_evidence(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The local in-process path shapes the prompt and materializes evidence.

        Routes through the canonical effect handler transport (patched at the
        boundary) and the canonical projection (local SQLite). The shaped prompt
        carries the inference-protocol /no_think directive + chat_template_kwargs;
        the materialized evidence row carries the ORIGINAL prompt (no directive).
        """
        captured_payloads: list[dict[str, Any]] = []

        def fake_post(
            *,
            endpoint_url: str,
            payload: dict[str, Any],
            timeout_seconds: float,
            extra_headers: dict[str, str] | None = None,
            runtime_profile: str | None = None,
        ) -> transport.ModelTransportResponse:
            captured_payloads.append(payload)
            return transport.ModelTransportResponse(
                status_code=200,
                json_body={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    "import pytest\n\n"
                                    "@pytest.mark.unit\n"
                                    "def test_normalize_status_ok():\n"
                                    "    assert normalize_status('OK') == 'ok'\n"
                                )
                            }
                        }
                    ],
                    "model": "Qwen3-Coder-30B",
                    "usage": {
                        "prompt_tokens": 18,
                        "completion_tokens": 44,
                        "total_tokens": 62,
                    },
                },
                latency_ms=37,
            )

        monkeypatch.setattr(transport, "probe_health", lambda *_a, **_k: True)
        monkeypatch.setattr(transport, "post_chat_completion", fake_post)
        monkeypatch.setattr(
            delegation_backend_resolution,
            "load_bifrost_backends",
            lambda **_: [
                {
                    "backend_id": "local-coder",
                    "endpoint_url": "http://inference.example:8000/v1/chat/completions",
                    "model_name": "Qwen3-Coder-30B",
                    "tier": "local",
                    # OMN-13161: per-backend output-token ceiling (router-resolved).
                    "max_tokens": 65536,
                    # OMN-13170: per-backend HTTP timeout (router-resolved, ms).
                    "timeout_ms": 300000,
                    "capabilities": ["test", "code_generation"],
                }
            ],
        )

        db_path = tmp_path / "delegation.sqlite"
        port = LocalDelegationDispatchPort(
            evidence_db_path=db_path,
            effect_process_boundary=False,
        )
        handler = HandlerDelegateSkill(dispatch_port=port)
        correlation_id = uuid4()
        original_prompt = "Write pytest unit tests for normalize_status."

        response = await handler.handle(
            ModelDelegateSkillRequest(
                prompt=original_prompt,
                task_type="test",
                source="codex",
                correlation_id=correlation_id,
            )
        )

        assert response.status == "completed"
        assert response.prompt_text == original_prompt
        assert "/no_think" not in response.prompt_text
        assert "def test_normalize_status_ok" in response.response
        assert response.metrics.input_tokens == 18
        assert response.metrics.output_tokens == 44
        # The shaped user message carries the /no_think directive + thinking off.
        assert len(captured_payloads) == 1
        messages = captured_payloads[0]["messages"]
        user_message = next(m for m in messages if m["role"] == "user")
        assert user_message["content"] == f"/no_think\n{original_prompt}"
        assert captured_payloads[0]["chat_template_kwargs"] == {
            "enable_thinking": False
        }

        conn = sqlite3.connect(str(db_path))
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM delegation_events WHERE correlation_id = ?",
                (str(correlation_id),),
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        assert row["prompt_text"] == original_prompt
        assert "/no_think" not in row["prompt_text"]
        assert "def test_normalize_status_ok" in row["response_text"]
        assert row["tokens_input"] == 18
        assert row["tokens_output"] == 44
