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
        )
        model = entry.to_model()
        assert isinstance(model, ModelChainDefinition)
        assert model.name == "test"
        assert model.head_topic == "onex.evt.test.v1"
        assert model.tail_table == "test_table"
        assert model.expected_fields == ["correlation_id"]

    def test_default_expected_fields(self) -> None:
        entry = ChainRegistryEntry(name="x", head_topic="t", tail_table="tt")
        assert entry.expected_fields == []


@pytest.mark.unit
class TestLoadRegistry:
    def test_loads_bundled_registry(self) -> None:
        chains = load_registry()
        assert len(chains) == 5
        names = {c.name for c in chains}
        assert names == {
            "registration",
            "pattern_learning",
            "delegation",
            "routing",
            "evaluation",
        }

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
                        }
                    ]
                }
            )
        )
        chains = load_registry(path=registry_file)
        assert len(chains) == 1
        assert chains[0].name == "custom"
        assert chains[0].expected_fields == ["id"]

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
        # registration also expects selected_agent
        projected_rows["registration"]["selected_agent"] = "agent-test"

        request = GoldenChainSweepRequest(chains=chains, projected_rows=projected_rows)
        result = NodeGoldenChainSweep().handle(request)

        assert result.overall_status == EnumSweepStatus.PASS
        assert result.chains_total == 5
        assert result.chains_passed == 5
