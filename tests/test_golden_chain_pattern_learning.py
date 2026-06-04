# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for pattern_learning: onex.evt.omniintelligence.pattern-stored.v1 → pattern_learning_artifacts."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_golden_chain_sweep.handlers.handler_golden_chain_sweep import (
    EnumChainStatus,
    EnumSweepStatus,
    GoldenChainSweepRequest,
    ModelChainDefinition,
    NodeGoldenChainSweep,
)
from omnimarket.nodes.node_golden_chain_sweep.registry import load_registry

_CHAIN = ModelChainDefinition(
    name="pattern_learning",
    head_topic="onex.evt.omniintelligence.pattern-stored.v1",
    tail_table="pattern_learning_artifacts",
    expected_fields=["correlation_id"],
)

_HANDLER = NodeGoldenChainSweep()


@pytest.mark.unit
class TestPatternLearningChainDefinition:
    """Verify the chain definition matches the golden_chains.yaml registry entry."""

    def test_head_topic_matches_registry(self) -> None:
        chains = load_registry()
        chain_map = {c.name: c for c in chains}
        assert "pattern_learning" in chain_map
        assert (
            chain_map["pattern_learning"].head_topic
            == "onex.evt.omniintelligence.pattern-stored.v1"
        )

    def test_tail_table_matches_registry(self) -> None:
        chains = load_registry()
        chain_map = {c.name: c for c in chains}
        assert chain_map["pattern_learning"].tail_table == "pattern_learning_artifacts"

    def test_expected_fields_include_correlation_id(self) -> None:
        chains = load_registry()
        chain_map = {c.name: c for c in chains}
        assert "correlation_id" in chain_map["pattern_learning"].expected_fields


@pytest.mark.unit
class TestPatternLearningChainValidation:
    """Sweep handler validates pattern_learning chain against projected rows."""

    def test_pass_when_row_present_with_required_fields(self) -> None:
        request = GoldenChainSweepRequest(
            chains=[_CHAIN],
            projected_rows={"pattern_learning": {"correlation_id": "corr-001"}},
        )
        result = _HANDLER.handle(request)
        assert result.overall_status == EnumSweepStatus.PASS
        assert result.chains_passed == 1
        assert result.chain_results[0].status == EnumChainStatus.PASS

    def test_fail_when_correlation_id_missing(self) -> None:
        request = GoldenChainSweepRequest(
            chains=[_CHAIN],
            projected_rows={"pattern_learning": {"other_field": "x"}},
        )
        result = _HANDLER.handle(request)
        assert result.overall_status == EnumSweepStatus.FAIL
        assert result.chain_results[0].status == EnumChainStatus.FAIL
        assert "correlation_id" in result.chain_results[0].missing_fields

    def test_timeout_when_no_row_projected(self) -> None:
        request = GoldenChainSweepRequest(
            chains=[_CHAIN],
            projected_rows={},
        )
        result = _HANDLER.handle(request)
        assert result.chain_results[0].status == EnumChainStatus.TIMEOUT
        assert result.overall_status == EnumSweepStatus.FAIL

    def test_gated_when_idle_gate_and_no_row(self) -> None:
        request = GoldenChainSweepRequest(
            chains=[_CHAIN],
            projected_rows={},
            idle_gate=True,
        )
        result = _HANDLER.handle(request)
        assert result.chain_results[0].status == EnumChainStatus.GATED
        assert result.overall_status == EnumSweepStatus.GATED
        assert result.chains_gated == 1
        assert result.chains_failed == 0

    def test_extra_fields_in_row_do_not_fail(self) -> None:
        request = GoldenChainSweepRequest(
            chains=[_CHAIN],
            projected_rows={
                "pattern_learning": {
                    "correlation_id": "corr-002",
                    "pattern_type": "delegation",
                    "confidence": 0.92,
                }
            },
        )
        result = _HANDLER.handle(request)
        assert result.chain_results[0].status == EnumChainStatus.PASS

    def test_chain_head_and_tail_carried_in_result(self) -> None:
        request = GoldenChainSweepRequest(
            chains=[_CHAIN],
            projected_rows={"pattern_learning": {"correlation_id": "corr-003"}},
        )
        result = _HANDLER.handle(request)
        cr = result.chain_results[0]
        assert cr.head_topic == "onex.evt.omniintelligence.pattern-stored.v1"
        assert cr.tail_table == "pattern_learning_artifacts"

    def test_registry_driven_sweep_passes_for_pattern_learning(self) -> None:
        chains = load_registry()
        projected_rows: dict[str, dict[str, object]] = {
            c.name: {"correlation_id": f"test-{c.name}"} for c in chains
        }
        # registration chain also expects selected_agent
        projected_rows["registration"]["selected_agent"] = "agent-test"
        # delegation chain expects compliance counters (OMN-10793)
        projected_rows["delegation"]["tokens_to_compliance"] = 0
        projected_rows["delegation"]["compliance_attempts"] = 1
        # evaluation chain expects session_id (not correlation_id) per contract.yaml
        projected_rows["evaluation"]["session_id"] = "test-evaluation"
        # OMN-12660 WS-G: sea_acceptance additional required fields
        projected_rows["sea_acceptance"]["task_type"] = "generate_onex_node"
        projected_rows["sea_acceptance"]["delegated_to"] = "claude-sonnet-4-6"
        # OMN-12660 WS-G: d3_local_routing additional required fields
        projected_rows["d3_local_routing"]["base_url"] = (
            "http://192.168.86.201:8000"  # onex-allow-internal-ip OMN-12660 reason="D3 sweep fixture: reference local-first endpoint"
        )
        projected_rows["d3_local_routing"]["model"] = "qwen3-coder-30b"
        # OMN-12660 WS-G: d1_d2_scaffold additional required fields
        projected_rows["d1_d2_scaffold"]["node_name"] = "NodeExampleCompute"
        projected_rows["d1_d2_scaffold"]["contract_passed"] = True
        projected_rows["d1_d2_scaffold"]["content"] = "class HandlerExample: ..."
        # OMN-12660 WS-G: d4_blank_content additional required fields
        projected_rows["d4_blank_content"]["content"] = "Generated node code..."
        projected_rows["d4_blank_content"]["model_used"] = "qwen3-coder-30b"
        # OMN-12660 WS-G: d9_wheel_module additional required fields
        projected_rows["d9_wheel_module"]["node_startup_ok"] = True
        # OMN-12660 WS-G: f1_publish_loop additional required fields
        projected_rows["f1_publish_loop"]["published_at"] = "2026-06-03T00:00:00Z"
        # OMN-12687 WS I-A: inference request/response round-trip fields
        projected_rows["delegation_inference_round_trip"].update(
            {
                "content": "Generated node code...",
                "model_used": "qwen3-coder-30b",
                "llm_call_id": "chatcmpl-proof",
                "prompt_tokens": 144,
                "completion_tokens": 593,
                "total_tokens": 737,
            }
        )
        # OMN-12687 WS I-A: terminal reducer materialization fields
        projected_rows["delegation_projection_materialization"].update(
            {
                "task_type": "research",
                "delegated_to": "qwen3-coder-30b",
                "model_name": "qwen3-coder-30b",
                "quality_gate_passed": True,
                "response_text": "Generated node code...",
                "tokens_input": 144,
                "tokens_output": 593,
                "tokens_to_compliance": 737,
                "compliance_attempts": 1,
            }
        )

        request = GoldenChainSweepRequest(
            chains=chains,
            projected_rows=projected_rows,
        )
        result = _HANDLER.handle(request)

        assert result.overall_status == EnumSweepStatus.PASS
        chain_statuses = {r.name: r.status for r in result.chain_results}
        assert chain_statuses["pattern_learning"] == EnumChainStatus.PASS
