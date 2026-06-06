# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for golden chain registry loading and fallback behavior."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_golden_chain_sweep.handlers.handler_golden_chain_sweep import (
    ModelChainDefinition,
)
from omnimarket.nodes.node_golden_chain_sweep.registry import (
    ChainRegistryEntry,
    load_registry,
)


@pytest.mark.unit
class TestChainRegistryEntry:
    def test_to_model_roundtrip(self) -> None:
        entry = ChainRegistryEntry(
            name="test",
            head_topic="onex.evt.test.v1",
            tail_table="test_table",
            expected_fields=["correlation_id"],
            proof_classification="proof-ready",
            replay_status="replay-proven",
            stages=[{"name": "head", "topic": "onex.evt.test.v1"}],
        )
        model = entry.to_model()
        assert isinstance(model, ModelChainDefinition)
        assert model.name == "test"
        assert model.head_topic == "onex.evt.test.v1"
        assert model.tail_table == "test_table"
        assert model.expected_fields == ["correlation_id"]
        assert model.proof_classification == "proof-ready"
        assert model.replay_status == "replay-proven"
        assert model.stages == [{"name": "head", "topic": "onex.evt.test.v1"}]

    def test_default_expected_fields(self) -> None:
        entry = ChainRegistryEntry(name="x", head_topic="t", tail_table="tt")
        assert entry.expected_fields == []
        assert entry.proof_classification == "diagnostic"
        assert entry.replay_status == "replay-not-applicable"
        assert entry.stages == []


@pytest.mark.unit
class TestLoadRegistry:
    def test_loads_bundled_registry(self) -> None:
        chains = load_registry()
        names = {c.name for c in chains}
        # Core chains (pre-OMN-12660)
        assert {
            "registration",
            "pattern_learning",
            "delegation",
            "routing",
            "evaluation",
        }.issubset(names)
        # OMN-12660 WS-G error + acceptance chains
        assert {
            "sea_acceptance",
            "d3_local_routing",
            "d1_d2_scaffold",
            "d4_blank_content",
            "d9_wheel_module",
            "f1_publish_loop",
            "delegation_inference_round_trip",
            "delegation_projection_materialization",
        }.issubset(names)
        assert len(chains) == 13

    def test_loads_custom_yaml(self, tmp_path: Path) -> None:
        registry_file = tmp_path / "golden_chains.yaml"
        registry_file.write_text(
            yaml.dump(
                {
                    "chains": [
                        {
                            "name": "custom",
                            "head_topic": "onex.evt.custom.v1",
                            "tail_table": "custom_table",
                            "expected_fields": ["id"],
                            "proof_classification": "proof-ready",
                            "replay_status": "runtime-observed-only",
                            "stages": [{"name": "custom-stage"}],
                        }
                    ]
                }
            )
        )
        chains = load_registry(path=registry_file)
        assert len(chains) == 1
        assert chains[0].name == "custom"
        assert chains[0].expected_fields == ["id"]
        assert chains[0].proof_classification == "proof-ready"
        assert chains[0].replay_status == "runtime-observed-only"
        assert chains[0].stages == [{"name": "custom-stage"}]

    def test_fallback_when_file_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.yaml"
        fallback = [
            ModelChainDefinition(
                name="fallback_chain",
                head_topic="t",
                tail_table="t",
            )
        ]
        result = load_registry(path=missing, fallback=fallback)
        assert result == fallback

    def test_fallback_on_invalid_yaml(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text("{ invalid yaml ][")
        fallback = [ModelChainDefinition(name="fb", head_topic="t", tail_table="tt")]
        result = load_registry(path=bad_file, fallback=fallback)
        assert result == fallback

    def test_fallback_on_missing_chains_key(self, tmp_path: Path) -> None:
        registry_file = tmp_path / "golden_chains.yaml"
        registry_file.write_text(yaml.dump({"other_key": []}))
        fallback = [ModelChainDefinition(name="fb", head_topic="t", tail_table="tt")]
        result = load_registry(path=registry_file, fallback=fallback)
        assert result == fallback

    def test_skips_malformed_entries_keeps_valid(self, tmp_path: Path) -> None:
        registry_file = tmp_path / "golden_chains.yaml"
        registry_file.write_text(
            yaml.dump(
                {
                    "chains": [
                        {
                            "name": "good",
                            "head_topic": "t.good",
                            "tail_table": "tbl_good",
                        },
                        {"missing_required": True},
                    ]
                }
            )
        )
        chains = load_registry(path=registry_file)
        assert len(chains) == 1
        assert chains[0].name == "good"

    def test_fallback_when_all_entries_malformed(self, tmp_path: Path) -> None:
        registry_file = tmp_path / "golden_chains.yaml"
        registry_file.write_text(yaml.dump({"chains": [{"bad": True}]}))
        fallback = [ModelChainDefinition(name="fb", head_topic="t", tail_table="tt")]
        result = load_registry(path=registry_file, fallback=fallback)
        assert result == fallback

    def test_bundled_chains_have_correct_topics(self) -> None:
        chains = load_registry()
        chain_map = {c.name: c for c in chains}
        assert (
            chain_map["registration"].head_topic
            == "onex.evt.omniclaude.routing-decision.v1"
        )
        assert chain_map["routing"].tail_table == "llm_routing_decisions"
        assert "correlation_id" in chain_map["delegation"].expected_fields
        # OMN-10793 — compliance counters are first-class chain-validated fields.
        assert "tokens_to_compliance" in chain_map["delegation"].expected_fields
        assert "compliance_attempts" in chain_map["delegation"].expected_fields
        assert (
            chain_map["delegation_inference_round_trip"].head_topic
            == "onex.cmd.omnibase-infra.delegation-inference-request.v1"
        )
        assert (
            chain_map["delegation_inference_round_trip"].tail_table
            == "event_bus:onex.evt.omnibase-infra.inference-response.v1"
        )
        assert (
            chain_map["delegation_projection_materialization"].tail_table
            == "delegation_events"
        )

    def test_i_a_proof_path_chains_are_diagnostic_until_live_packet(self) -> None:
        chain_map = {c.name: c for c in load_registry()}

        inference = chain_map["delegation_inference_round_trip"]
        assert inference.proof_classification == "diagnostic"
        assert inference.replay_status == "replay-not-applicable"
        assert [stage["name"] for stage in inference.stages] == [
            "inference_request",
            "inference_response",
        ]

        projection = chain_map["delegation_projection_materialization"]
        assert projection.proof_classification == "diagnostic"
        assert projection.replay_status == "replay-proven"
        assert [stage["name"] for stage in projection.stages] == [
            "delegation_completed",
            "delegation_events_row",
        ]

    def test_empty_fallback_default(self, tmp_path: Path) -> None:
        missing = tmp_path / "no.yaml"
        result = load_registry(path=missing)
        assert result == []


@pytest.mark.unit
class TestRegistryIntegrationWithSweep:
    """Run a sweep using chains loaded from the registry."""

    def test_registry_chains_drive_sweep(self) -> None:
        from omnimarket.nodes.node_golden_chain_sweep.handlers.handler_golden_chain_sweep import (
            EnumSweepStatus,
            GoldenChainSweepRequest,
            NodeGoldenChainSweep,
        )

        chains = load_registry()
        projected_rows = {c.name: {"correlation_id": f"test-{c.name}"} for c in chains}
        # registration also expects selected_agent.
        projected_rows["registration"]["selected_agent"] = "agent-test"
        # delegation expects compliance counters (OMN-10793).
        projected_rows["delegation"]["tokens_to_compliance"] = 0
        projected_rows["delegation"]["compliance_attempts"] = 1
        # evaluation expects session_id (not correlation_id) per contract.yaml
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

        request = GoldenChainSweepRequest(chains=chains, projected_rows=projected_rows)
        result = NodeGoldenChainSweep().handle(request)

        assert result.overall_status == EnumSweepStatus.PASS
        assert result.chains_total == 13
        assert result.chains_passed == 13
