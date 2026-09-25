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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from omnibase_core.models.delegation.wire import ModelDelegationProvenance
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.runtime.dispatch_envelope_context import bind_dispatch_envelope

from omnimarket.delegation.response_contract_instruction import (
    render_extraction_marker_instruction,
)
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.judge.handler_judge_adequacy import (
    HandlerJudgeAdequacy,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.routing import delegation_backend_resolution
from tests.fixtures.judge_inference import CannedAdequacyBridge


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
        execution_timeout_seconds: int,
        terminal_delivery_margin_seconds: int,
        quality_contract_mode: str,
        acceptance_criteria: tuple[str, ...],
        tenant_id: str | None,
        backend_id: str | None = None,
        response_contract: dict[str, object] | None = None,
        # OMN-15482: completion-shaping parameters added to
        # ``ProtocolDelegationDispatchPort``. Recorded rather than merely
        # accepted so this golden chain keeps proving what the handler forwards.
        system_prompt: str | None = None,
        temperature: float | None = None,
        response_format: dict[str, object] | None = None,
        provenance: ModelDelegationProvenance | None = None,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "prompt": prompt,
                "task_type": task_type,
                "correlation_id": correlation_id,
                "backend_id": backend_id,
                "response_contract": response_contract,
                "system_prompt": system_prompt,
                "temperature": temperature,
                "response_format": response_format,
                "provenance": provenance,
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
        assert port.calls[0]["provenance"] is None

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

    async def test_completed_chain_reports_queue_and_execution_separately(
        self,
    ) -> None:
        """OMN-18852: the success chain carries both accounting facts.

        A caller reading only wall clock cannot tell a slow delegation from a
        queued one. Live on 2026-09-19 a control run took 181 s of which the
        inference was 1.559 s, and the terminal said nothing about the other
        179 s.
        """
        published_at = datetime.now(UTC) - timedelta(seconds=45)
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
                correlation_id=uuid4(),
                published_at=published_at,
            )
        )

        assert response.status == "completed"
        assert response.queue_wait_ms is not None
        assert response.queue_wait_ms >= 45_000
        assert response.execution_duration_ms is not None
        assert response.execution_duration_ms < 5_000
        # Both survive the terminal-variant conversion, which round-trips
        # through model_dump -- a field that is dropped there is a field the
        # projection never sees.
        assert "queue_wait_ms" in response.model_dump()
        assert "execution_duration_ms" in response.model_dump()

    async def test_error_chain_reports_queue_and_execution_separately(self) -> None:
        """OMN-18852: the failure terminal carries the same two facts.

        A failure behind a long queue is the case most likely to be
        misdiagnosed as a broken backend, so the failed terminal is exactly
        where the split matters.
        """

        class _RaisingPort:
            async def dispatch(self, **_: object) -> dict[str, object]:
                raise RuntimeError("backend unavailable")

        published_at = datetime.now(UTC) - timedelta(seconds=90)
        handler = HandlerDelegateSkill(dispatch_port=_RaisingPort())

        response = await handler.handle(
            ModelDelegateSkillRequest(
                prompt="do work",
                task_type="code_generation",
                source="claude-code",
                correlation_id=uuid4(),
                published_at=published_at,
            )
        )

        assert response.status == "failed"
        assert "backend unavailable" in response.error_message
        assert response.queue_wait_ms is not None
        assert response.queue_wait_ms >= 90_000
        assert response.execution_duration_ms is not None
        assert response.execution_duration_ms < 5_000

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
                                    "### ANSWER\n"
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
        # OMN-13849: `test` is a judge-combinable class and the local path applies
        # the 0.85 required bar. Inject a passing judge so the single-tier `test`
        # answer clears the bar (the combine lifts the ~0.733 deterministic-only
        # score) and the chain stays a single-attempt COMPLETED — this test asserts
        # the /no_think shaping + one inference call, not the judge-veto path.
        port = LocalDelegationDispatchPort(
            evidence_db_path=db_path,
            effect_process_boundary=False,
            judge=HandlerJudgeAdequacy(
                inference_bridge=CannedAdequacyBridge(adequacy_score=0.95)
            ),
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
        # OMN-18349: a text deliverable's extraction-marker sentence opens the
        # user turn, before the caller's own prompt; the system message keeps
        # the full instruction so evidence reads it conveyed.
        marker_sentence = render_extraction_marker_instruction("### ANSWER")
        system_message = next(m for m in messages if m["role"] == "system")
        assert marker_sentence in system_message["content"]
        assert (
            user_message["content"]
            == f"/no_think\n{marker_sentence}\n\n{original_prompt}"
        )
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


# ---------------------------------------------------------------------------
# OMN-18887: a REDELIVERED command must not re-run and re-bill the inference
#
# The consume path auto-commits and never calls commit, so delivery is
# at-least-once by contract, and since OMN-18852 four records run in flight at
# once. A rebalance, a crash or a rewind therefore re-runs a delegation end to
# end: a fresh inference is issued, the provider is called again, and a second
# billing row is written. Nothing today notices the correlation was served --
# `descriptor.idempotent: false`, and `handle()` goes straight to dispatch with
# no lookup of any kind.
#
# It is also the first way this system can double-bill without anything
# failing. Every prior cost surprise was a failed rung retried by the
# escalation ladder, visible in `attempts` on the terminal. A redelivery
# produces a second, independent, apparently-clean success.
#
# These live here rather than beside the handler unit tests deliberately: a
# handler-level test with a mocked port can observe neither a second inference
# nor a second billing row. `_StubDispatchPort.calls` is the observation.
# ---------------------------------------------------------------------------


class TestDelegateSkillRedeliveryIsIdempotent:
    """One DELIVERED RECORD bills once, however many times it is delivered."""

    @staticmethod
    def _completed_result() -> dict[str, object]:
        return {
            "status": "completed",
            "content": "def parse(): ...",
            "delegated_to": "local-runtime",
            "model_name": "qwen-coder",
            "quality_gate_passed": True,
            "quality_score": 0.91,
        }

    @staticmethod
    def _request(correlation_id: UUID) -> ModelDelegateSkillRequest:
        return ModelDelegateSkillRequest(
            prompt="generate a parser for the config file",
            task_type="code_generation",
            source="claude-code",
            correlation_id=correlation_id,
        )

    @staticmethod
    def _delivery(
        correlation_id: UUID, envelope_id: UUID
    ) -> ModelEventEnvelope[object]:
        """One delivered record, as the runtime binds it around a dispatch.

        ``envelope_id`` is the delivery identity. After OMN-18958 it is the
        wire message id, so a redelivery of one record carries the SAME value
        and a genuinely new command carries a different one.
        """
        return ModelEventEnvelope[object](
            envelope_id=envelope_id,
            payload={},
            correlation_id=correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type="omnimarket.delegate-skill",
            source_tool="omn18887-test",
        )

    @pytest.mark.unit
    async def test_a_redelivered_record_dispatches_the_inference_once(self) -> None:
        """AC1/AC2. The SAME record delivered twice must reach the provider once.

        `calls` is the billing proxy: one entry is one inference issued and one
        cost row. Two entries for one delivery is the double-bill.
        """
        correlation_id = uuid4()
        envelope_id = uuid4()
        port = _StubDispatchPort(self._completed_result())
        handler = HandlerDelegateSkill(dispatch_port=port)
        request = self._request(correlation_id)

        for _ in range(2):
            with bind_dispatch_envelope(self._delivery(correlation_id, envelope_id)):
                await handler.handle(request)

        assert len(port.calls) == 1, (
            f"one record was delivered twice and the inference was issued "
            f"{len(port.calls)} times; each one is a provider call and a billing "
            "row for a delivery that was already served (OMN-18887)"
        )

    @pytest.mark.unit
    async def test_the_redelivered_record_still_answers_its_caller(self) -> None:
        """AC3. Suppressing the work must not suppress the answer.

        A suppression that returns None publishes no terminal at all, which
        converts a double-bill into the missing-envelope defect OMN-15504
        exists to prevent.
        """
        correlation_id = uuid4()
        envelope_id = uuid4()
        port = _StubDispatchPort(self._completed_result())
        handler = HandlerDelegateSkill(dispatch_port=port)
        request = self._request(correlation_id)

        with bind_dispatch_envelope(self._delivery(correlation_id, envelope_id)):
            first = await handler.handle(request)
        with bind_dispatch_envelope(self._delivery(correlation_id, envelope_id)):
            second = await handler.handle(request)

        assert second is not None, (
            "the redelivered record returned None, so the wiring publishes no "
            "terminal and the caller waits out its budget for an answer that "
            "was already computed (OMN-15504, AC3)"
        )
        assert type(second) is type(first), (
            "the re-emitted terminal is a different class from the original, so "
            "it would publish on the other terminal topic and invert the "
            f"outcome: {type(first).__name__} then {type(second).__name__}"
        )
        assert second.correlation_id == correlation_id

    @pytest.mark.unit
    async def test_a_reused_correlation_on_a_new_record_still_dispatches(self) -> None:
        """The control that decided the key, and the reason it is not correlation.

        Correlation is the RETRY identity: a caller may reuse one, and this
        repo's own quota-seam fixtures do, driving a forced failure and a
        success under a single correlation. Keyed on correlation the second of
        those is answered with the first one's stale terminal and never runs.

        Two DIFFERENT delivered records sharing one correlation are two
        commands and must both be dispatched.
        """
        correlation_id = uuid4()
        port = _StubDispatchPort(self._completed_result())
        handler = HandlerDelegateSkill(dispatch_port=port)
        request = self._request(correlation_id)

        for _ in range(2):
            with bind_dispatch_envelope(self._delivery(correlation_id, uuid4())):
                await handler.handle(request)

        assert len(port.calls) == 2, (
            "two distinct delivered records sharing one correlation were "
            "collapsed into a single dispatch; the claim is keyed on the retry "
            "identity rather than the delivery identity"
        )

    @pytest.mark.unit
    async def test_a_direct_call_with_no_delivery_is_never_suppressed(self) -> None:
        """No bound envelope means no delivery, so there is nothing to suppress.

        The bus-less CLI and the two other handler construction sites call this
        handler directly. None of those is a redelivery, and claiming against a
        substitute key there would suppress real work.
        """
        correlation_id = uuid4()
        port = _StubDispatchPort(self._completed_result())
        handler = HandlerDelegateSkill(dispatch_port=port)
        request = self._request(correlation_id)

        await handler.handle(request)
        await handler.handle(request)

        assert len(port.calls) == 2, (
            "a direct call with no delivered record was suppressed; the claim "
            "must key on a delivery that exists, never on a stand-in"
        )
