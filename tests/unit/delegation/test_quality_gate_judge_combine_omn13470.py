# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13470: LLM-judge adequacy combined into the delegation quality gate.

This suite proves the OMN-13470 fix by DOGFOODING THE LIVE ARCHITECTURE — never
by mocking the inference bridge / model router / routing resolution / registry /
dispatch. The deleted version injected a ``_FakeBridge`` that accepted any
``model_key`` (including the delegation TIER name ``cheap_cloud``) and patched
``httpx.Client``; that is exactly why the live ``ValueError: Unknown model_key:
'cheap_cloud'`` bug shipped GREEN. Both of those are BANNED here.

What replaces them:

  * the judge resolves a CONCRETE model id + COMPLETE verbatim endpoint +
    secret_ref from the REAL routing contract via ``resolve_delegation_backend``
    (``RoutingResolvedJudgeInferenceAdapter``). A tier name can never reach the
    inference layer.
  * the inference call is either a REAL z.ai GLM cloud call (gated on
    ``LLM_GLM_API_KEY`` being configured) or a RECORDED-FROM-A-REAL-CALL replay
    (``RecordedJudgeReplayAdapter``) that HARD-REJECTS a tier name as ``model_key``
    — so the replay cannot mask the regression the live bug shipped on.
  * the end-to-end chain runs over the REAL in-memory bus (``EventBusInmemory``,
    the same transport RuntimeLocal uses for offline proof) with REAL handler
    registration: orchestrator FSM -> routing intent -> inference intent ->
    quality-gate intent (judge EFFECT + combine) -> terminal gate result.

LAN boundary note (macOS LAN-grant constraint): the local vLLM ``.201`` backends
are LAN-only and unreachable from a uv-managed interpreter here, so the judge is
pinned to the cloud ``cloud-glm`` backend (internet-reachable). The Kafka/Postgres
transport is replaced by ``EventBusInmemory`` exactly as RuntimeLocal does — that
substitution is the only mocked boundary and it is the architecture's own offline
bus, not a hand-rolled fake of the inference path.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.delegation.wire import (
    ModelInferenceIntent,
    ModelQualityGateIntent,
    ModelRoutingIntent,
)

from omnimarket.events.delegation_judge_verdict import (
    EnumDelegationJudgeVerdict,
    ModelDelegationJudgeVerdictEvent,
)
from omnimarket.inference.secret_store_resolver import (
    clear_secret_store_resolver_cache,
)
from omnimarket.nodes.node_delegation_orchestrator.contract_topics import (
    TOPIC_ID_DELEGATION_COMPLETED,
    TOPIC_ID_DELEGATION_FAILED,
)
from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_event import (
    ModelDelegationEvent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate_intent import (
    HandlerQualityGateIntent,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.judge.adapter_routing_resolved_judge import (
    RoutingResolvedJudgeInferenceAdapter,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.judge.handler_judge_adequacy import (
    HandlerJudgeAdequacy,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_routing_intent import (
    HandlerRoutingIntent,
)
from omnimarket.routing.delegation_backend_resolution import (
    resolve_delegation_backend,
)
from tests.fixtures.judge_inference import RecordedJudgeReplayAdapter

_FIXTURES_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "judge_inference"

_CODE_GEN_DOD = (
    "compiles_without_errors",
    "final_artifact_only",
    "passes_existing_tests",
)
_CODE_GEN_HEUR = (
    "no_refusal",
    "follows_codebase_conventions",
    "no_obvious_regressions",
)

# A good-but-mechanically-incomplete code answer: it compiles, is a single
# artifact, and is non-empty (deterministic floor passes), but it carries none of
# the convention/regression markers, so the deterministic-only graded score is
# ~0.733 and fails the 0.85 bar without the judge.
_GOOD_CODE = "```python\ndef add(a: int, b: int) -> int:\n    return a + b\n```"

_REFUSAL = "I cannot help with that request."

# Self-contained bifrost contract for the real-bus chain's routing resolution.
# Same shape as the committed src/omnimarket/configs/bifrost_delegation.yaml — a
# concrete cloud backend with a COMPLETE verbatim endpoint_url so routing resolves
# without a host overlay (CI has no ~/.omninode/delegation/bifrost_overrides.yaml).
_BIFROST_CONTRACT_CODE = (
    "config_version: '2.0.0'\n"
    "schema_version: bifrost_delegation.v1\n"
    "backends:\n"
    "  - backend_id: cloud-gemini-flash\n"
    '    endpoint_url: "https://example.test/v1/chat/completions"\n'
    '    model_name: "gemini-2.5-flash-lite"\n'
    "    tier: cheap_cloud\n"
    "    timeout_ms: 30000\n"
    "    max_tokens: 8192\n"
    "    capabilities: [code_generation, simple_tasks, document, summarization]\n"
    "routing_rules:\n"
    '  - rule_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"\n'
    "    priority: 10\n"
    "    task_class: code_generation\n"
    '    task_class_contract_version: "1.0.0"\n'
    '    backend_policy_version: "2.0.0"\n'
    "    match_operation_types: [chat_completion]\n"
    "    match_capabilities: [code_generation]\n"
    "    backend_ids: [cloud-gemini-flash]\n"
    "    fallback_policy:\n"
    "      action: escalate_to_next_tier\n"
    "      max_retries: 1\n"
    "      on_exhaust: return_error\n"
    '    shadow_policy_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"\n'
    "default_backends:\n"
    "  - cloud-gemini-flash\n"
    "circuit_breaker:\n"
    "  failure_threshold: 5\n"
    "  window_seconds: 30\n"
    "failover:\n"
    "  max_attempts: 3\n"
    "  backoff_base_ms: 500\n"
    "shadow_mode:\n"
    "  enabled: false\n"
    '  policy_version: "test"\n'
    "  log_sample_rate: 1.0\n"
    "  comparison_logging_enabled: true\n"
    "  max_shadow_latency_ms: 5.0\n"
)

_GLM_KEY_CONFIGURED = bool(os.environ.get("LLM_GLM_API_KEY", "").strip())
_LIVE_JUDGE_ENABLED = (
    os.environ.get("OMN_ALLOW_LIVE_JUDGE_CALL", "").lower() == "true"
    and _GLM_KEY_CONFIGURED
)


def _code_gate_input(content: str) -> ModelQualityGateInput:
    return ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="code_generation",
        llm_response_content=content,
        dod_deterministic=_CODE_GEN_DOD,
        dod_heuristic=_CODE_GEN_HEUR,
    )


# ---------------------------------------------------------------------------
# Pure-reducer combine unit tests (no inference at all)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGateCombineUnit:
    def test_good_incomplete_code_fails_without_judge(self) -> None:
        """Deterministic-only: a good answer scores below the 0.85 bar."""
        result = quality_gate_delta(_code_gate_input(_GOOD_CODE))
        assert result.quality_score < 0.85
        assert result.score_source != "combined"

    def test_good_incomplete_code_passes_with_high_judge(self) -> None:
        """A high judge adequacy score lifts the combined score over the bar."""
        result = quality_gate_delta(
            _code_gate_input(_GOOD_CODE), judge_adequacy_score=0.95
        )
        assert result.passed is True
        assert result.score_source == "combined"
        assert result.quality_score >= 0.85

    def test_low_judge_score_keeps_combined_below_bar(self) -> None:
        result = quality_gate_delta(
            _code_gate_input(_GOOD_CODE), judge_adequacy_score=0.30
        )
        assert result.score_source == "combined"
        assert result.quality_score < 0.85

    def test_refusal_blocked_despite_maximal_judge(self) -> None:
        """OMN-13409 invariant: a refusal hard-blocks before the judge combine."""
        result = quality_gate_delta(
            _code_gate_input(_REFUSAL), judge_adequacy_score=0.99
        )
        assert result.passed is False
        assert result.fail_category == "fail_deterministic"
        assert result.score_source != "combined"

    def test_empty_blocked_despite_maximal_judge(self) -> None:
        result = quality_gate_delta(_code_gate_input(""), judge_adequacy_score=0.99)
        assert result.passed is False
        assert result.fail_category == "fail_deterministic"


# ---------------------------------------------------------------------------
# Routing-authority resolution: the judge resolves a CONCRETE model, not a tier
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestJudgeResolvesConcreteModelNotTier:
    def test_routing_authority_resolves_concrete_cloud_glm_backend(self) -> None:
        """OMN-13470 root cause: the judge backend is a CONCRETE cloud model.

        The committed routing contract's ``cloud-glm`` backend carries a COMPLETE
        verbatim endpoint URL, a concrete model_name (NOT a tier label), and a
        logical secret_ref. This is the resolution the judge rides — proving no
        tier name ever reaches the inference layer.
        """
        backend = resolve_delegation_backend("judge_adequacy", backend_id="cloud-glm")
        assert backend.backend_id == "cloud-glm"
        # The model id is a concrete served model, never the tier label.
        assert backend.tier not in backend.model_id
        assert backend.model_id not in {"cheap_cloud", "cheap_frontier", "local"}
        # The endpoint is a COMPLETE chat-completions URL (verbatim, OMN-12815).
        assert backend.endpoint_ref.startswith("https://")
        assert backend.endpoint_ref.endswith("/chat/completions")
        # The secret is carried as a logical ref only; never a literal value.
        assert backend.secret_ref == "llm.glm.api_key"

    def test_adapter_resolves_concrete_model_id(self) -> None:
        adapter = RoutingResolvedJudgeInferenceAdapter()
        model_id = adapter.resolved_model_id()
        assert model_id not in {"cheap_cloud", "cheap_frontier", "local", "unknown"}
        assert model_id  # non-empty concrete id

    @pytest.mark.asyncio
    async def test_recorded_replay_rejects_tier_name_as_model_key(self) -> None:
        """The replay fixture cannot mask the bug: a tier name fails closed.

        This is the guard the deleted ``_FakeBridge`` lacked — it accepted ANY
        model_key, so the tier-name bug passed its tests while failing live.
        """
        replay = RecordedJudgeReplayAdapter()
        with pytest.raises(ValueError, match="Unknown model_key: 'cheap_cloud'"):
            await replay.infer(
                model_key="cheap_cloud",
                system_prompt="s",
                user_prompt="u",
                timeout_seconds=5.0,
            )


# ---------------------------------------------------------------------------
# Judge EFFECT handler over the RECORDED-FROM-REAL-CALL replay (deterministic CI)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestJudgeAdequacyEffectReplay:
    @pytest.mark.asyncio
    async def test_judge_scores_via_recorded_real_response(self) -> None:
        """Judge produces a real, non-failed verdict from a recorded GLM response.

        The replay adapter is handed the concrete resolved model id by the judge
        (resolved from the routing authority), proving the model_key is concrete.
        """
        replay = RecordedJudgeReplayAdapter()
        judge = HandlerJudgeAdequacy(inference_bridge=replay)
        verdict = await judge.score(
            correlation_id=uuid4(),
            task_type="code_generation",
            prompt="implement add",
            candidate_output=_GOOD_CODE,
            acceptance_criteria=("compiles_without_errors", "final_artifact_only"),
        )
        assert isinstance(verdict, ModelDelegationJudgeVerdictEvent)
        assert verdict.verdict is not EnumDelegationJudgeVerdict.JUDGE_FAILED
        assert verdict.actual_score is not None
        assert verdict.actual_score > 0.0
        # The concrete model id was handed to the inference layer (NOT a tier).
        assert len(replay.calls) == 1
        assert replay.calls[0]["model_key"] not in {"cheap_cloud", "local"}
        # Provenance: the verdict records the concrete model id, not the tier.
        assert verdict.judge_model not in {"cheap_cloud", "cheap_frontier"}
        # Replay identity: the recorded event hash recomputes deterministically.
        assert verdict.event_hash == verdict.compute_event_hash()

    @pytest.mark.asyncio
    async def test_judge_failure_is_failclosed_not_zero(self) -> None:
        """An unreachable judge backend fails CLOSED to JUDGE_FAILED (no zero)."""

        class _UnreachableAdapter(RecordedJudgeReplayAdapter):
            async def infer(  # type: ignore[override]
                self,
                model_key: str,
                system_prompt: str,
                user_prompt: str,
                timeout_seconds: float,
                temperature: float | None = None,
            ) -> str:
                # Still rejects a tier name first (inherited guard), then raises a
                # real transport-style failure for a concrete model key.
                if model_key in {"cheap_cloud", "local"}:
                    raise ValueError(f"Unknown model_key: {model_key!r}")
                raise RuntimeError("judge endpoint unreachable")

        judge = HandlerJudgeAdequacy(inference_bridge=_UnreachableAdapter())
        verdict = await judge.score(
            correlation_id=uuid4(),
            task_type="code_generation",
            prompt="implement add",
            candidate_output=_GOOD_CODE,
        )
        assert verdict.verdict is EnumDelegationJudgeVerdict.JUDGE_FAILED
        assert verdict.actual_score is None
        assert verdict.failure_kind == "JUDGE_LLM_CALL_FAILED"


# ---------------------------------------------------------------------------
# Live REAL cloud judge call (gated on OMN_ALLOW_LIVE_JUDGE_CALL + GLM key)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.skipif(
    not _LIVE_JUDGE_ENABLED,
    reason="Requires OMN_ALLOW_LIVE_JUDGE_CALL=true + LLM_GLM_API_KEY configured",
)
class TestJudgeAdequacyEffectLiveCloud:
    @pytest.fixture(autouse=True)
    def _wire_secret_resolver(self) -> None:
        """Map the logical secret_ref to the env var via the lane resolver config.

        This is the SAME ModelSecretResolverConfig wiring the deployed lane uses
        (ONEX_SECRET_RESOLVER_CONFIG_PATH); not a mock — it resolves the real key.
        """
        cfg = _FIXTURES_ROOT / "live_secret_resolver.yaml"
        cfg.write_text(
            "mappings:\n"
            '  - logical_name: "llm.glm.api_key"\n'
            "    source:\n"
            '      source_type: "env"\n'
            '      source_path: "LLM_GLM_API_KEY"\n'
        )
        prior = os.environ.get("ONEX_SECRET_RESOLVER_CONFIG_PATH")
        os.environ["ONEX_SECRET_RESOLVER_CONFIG_PATH"] = str(cfg)
        clear_secret_store_resolver_cache()
        yield
        if prior is None:
            os.environ.pop("ONEX_SECRET_RESOLVER_CONFIG_PATH", None)
        else:
            os.environ["ONEX_SECRET_RESOLVER_CONFIG_PATH"] = prior
        clear_secret_store_resolver_cache()
        cfg.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_real_glm_judge_returns_non_failed_verdict(self) -> None:
        judge = HandlerJudgeAdequacy()  # production default: routing-resolved adapter
        verdict = await judge.score(
            correlation_id=uuid4(),
            task_type="code_generation",
            prompt="implement add(a,b) returning a+b",
            candidate_output=_GOOD_CODE,
            acceptance_criteria=("compiles_without_errors", "final_artifact_only"),
        )
        assert verdict.verdict is not EnumDelegationJudgeVerdict.JUDGE_FAILED
        assert verdict.actual_score is not None
        assert verdict.actual_score > 0.0
        assert verdict.judge_model not in {"cheap_cloud", "cheap_frontier"}


# ---------------------------------------------------------------------------
# REAL in-memory bus end-to-end chain (EventBusInmemory + real handlers)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestJudgeCombineRealBusChain:
    """Orchestrator FSM -> routing -> inference -> gate(judge) -> terminal, on a
    REAL in-memory bus (``EventBusInmemory``) with REAL routing resolution + REAL
    handler registration.

    The judge inference is the recorded-from-real-call replay (concrete model id
    pinned); the LLM endpoint is exercised for real in the live-gated suite above.
    The terminal delegation events are PUBLISHED OVER THE BUS and consumed by a
    real subscriber, proving a valid terminal event actually lands on the bus
    (the OMN-13140 all-tiers-failed-terminal-must-emit invariant).

    The routing reducer resolves a concrete backend from a SELF-CONTAINED bifrost
    contract pointed to by BIFROST_CONTRACT_PATH — REAL routing resolution, but
    host-independent (CI has no ~/.omninode overlay). The contract carries the
    same shape the committed bifrost config uses; resolution is real, not mocked.
    """

    @pytest.fixture(autouse=True)
    def _bifrost_contract(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> object:
        import omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing as _h

        _h._config = None
        _h._load_bifrost_endpoints.cache_clear()
        contract_path = tmp_path / "bifrost_delegation.yaml"
        contract_path.write_text(_BIFROST_CONTRACT_CODE, encoding="utf-8")
        monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
        yield
        _h._config = None
        _h._load_bifrost_endpoints.cache_clear()

    async def _drive_chain(
        self,
        *,
        llm_content: str,
        judge_adapter: RecordedJudgeReplayAdapter,
    ) -> tuple[object, list[object], HandlerDelegationWorkflow, list[str]]:
        bus = EventBusInmemory()
        await bus.start()
        consumed_topics: list[str] = []

        async def _on_terminal(message: object) -> None:
            # Decode the bus envelope exactly as a real consumer would.
            envelope = json.loads(message.value.decode("utf-8"))  # type: ignore[attr-defined]
            consumed_topics.append(str(envelope["topic"]))

        # Real subscription over the bus for both terminal topics.
        await bus.subscribe(
            TOPIC_ID_DELEGATION_COMPLETED,
            on_message=_on_terminal,
            group_id="omn13470-test",
        )
        await bus.subscribe(
            TOPIC_ID_DELEGATION_FAILED,
            on_message=_on_terminal,
            group_id="omn13470-test",
        )

        workflow = HandlerDelegationWorkflow(workflows={})
        routing_handler = HandlerRoutingIntent()
        gate_handler = HandlerQualityGateIntent(
            judge=HandlerJudgeAdequacy(inference_bridge=judge_adapter)
        )

        request = ModelDelegationRequest(
            prompt="Implement an add(a, b) function.",
            task_type="code_generation",
            correlation_id=uuid4(),
            max_tokens=512,
            emitted_at=datetime.now(UTC),
        )

        # --- orchestrator FSM start: emit routing intent ---
        routing_intents = workflow.handle_delegation_request(request)
        assert isinstance(routing_intents[0], ModelRoutingIntent)

        # --- routing reducer resolves a concrete backend (REAL resolution) ---
        decision = routing_handler.handle(routing_intents[0])
        inference_intents = workflow.handle_routing_decision(decision)
        assert isinstance(inference_intents[0], ModelInferenceIntent)

        # --- inference effect produces the candidate (recorded artifact) ---
        # The delegated-tier inference content is a recorded artifact so the chain
        # is deterministic; the JUDGE call (the OMN-13470 surface) rides the
        # recorded-from-real GLM response carrying a CONCRETE model_key.
        response = _RecordedInferenceResponse.build(inference_intents[0], llm_content)
        gate_intents = workflow.handle_inference_response(response)
        assert isinstance(gate_intents[0], ModelQualityGateIntent)

        # --- quality-gate intent: judge EFFECT + combine, emit gate result ---
        gate_output = await gate_handler.handle_async(gate_intents[0])
        gate_result = next(
            e
            for e in gate_output.events
            if type(e).__name__ == "ModelQualityGateResult"
        )
        judge_verdicts = [
            e
            for e in gate_output.events
            if isinstance(e, ModelDelegationJudgeVerdictEvent)
        ]
        assert judge_verdicts, "judge verdict event must be emitted"

        # --- terminal: publish the gate-driven terminal events OVER THE BUS ---
        terminal_events = workflow.handle_gate_result(gate_result)
        for ev in terminal_events:
            if isinstance(ev, ModelDelegationEvent):
                await bus.publish(
                    ev.topic,
                    key=str(request.correlation_id).encode(),
                    value=json.dumps(
                        {
                            "topic": ev.topic,
                            "correlation_id": str(request.correlation_id),
                            "payload": ev.payload.model_dump(mode="json"),
                        }
                    ).encode("utf-8"),
                )
        await asyncio.sleep(0.05)
        await bus.shutdown()

        return gate_result, terminal_events, workflow, consumed_topics

    @pytest.mark.asyncio
    async def test_good_code_completes_via_combined_score_over_bus(self) -> None:
        gate_result, _terminal, workflow, consumed = await self._drive_chain(
            llm_content=_GOOD_CODE,
            judge_adapter=RecordedJudgeReplayAdapter(),
        )
        assert gate_result.score_source == "combined"  # type: ignore[attr-defined]
        assert TOPIC_ID_DELEGATION_COMPLETED in consumed, (
            "good code answer must publish a terminal delegation-completed event "
            f"over the bus; consumed={consumed}"
        )
        wf = workflow.workflows[
            UUID(str(gate_result.correlation_id))  # type: ignore[attr-defined]
        ]
        assert wf.state == EnumDelegationState.COMPLETED

    @pytest.mark.asyncio
    async def test_refusal_not_completed_even_with_high_judge_over_bus(self) -> None:
        gate_result, _terminal, _workflow, consumed = await self._drive_chain(
            llm_content="I cannot complete this task.",
            judge_adapter=RecordedJudgeReplayAdapter(),
        )
        assert gate_result.passed is False  # type: ignore[attr-defined]
        assert TOPIC_ID_DELEGATION_COMPLETED not in consumed, (
            f"refusal must NOT complete even with a high judge score; consumed={consumed}"
        )
        # A terminal FAILED event must STILL be emitted (fail-closed, OMN-13140).
        assert TOPIC_ID_DELEGATION_FAILED in consumed, (
            "a refusal terminal must fail closed to a valid delegation-failed "
            f"event landing on the bus; consumed={consumed}"
        )


class _RecordedInferenceResponse:
    """Build the inference-response model from a recorded candidate artifact.

    The DELEGATED-tier inference content is a recorded artifact (the chain's
    determinism anchor); the OMN-13470 surface under test is the JUDGE inference,
    which rides the recorded-from-real GLM response with a concrete model_key.
    """

    @staticmethod
    def build(intent: ModelInferenceIntent, content: str) -> object:
        from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
            ModelInferenceResponseData,
        )

        return ModelInferenceResponseData(
            correlation_id=intent.correlation_id,
            content=content,
            model_used=intent.model,
            llm_call_id=str(uuid4()),
            latency_ms=12,
            prompt_tokens=10,
            completion_tokens=8,
            total_tokens=18,
        )


# Keep the recorded fixture referenced so a stale path fails loudly at import.
assert (_FIXTURES_ROOT / "glm_code_adequacy_pass.json").is_file()
assert json.loads((_FIXTURES_ROOT / "glm_code_adequacy_pass.json").read_text())[
    "resolved_model_id"
]
