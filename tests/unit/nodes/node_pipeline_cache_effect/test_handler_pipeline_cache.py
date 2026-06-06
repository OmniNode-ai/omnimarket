# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_pipeline_cache_effect.

Tests cover:
- Contract YAML structure and topic naming
- Cache miss: runs both compute nodes and writes cache files
- Cache hit: returns cached result without re-running compute nodes
- Regression: second run is a cache hit with identical output
- Cache key is deterministic for the same inputs
- Corrupt cache entry treated as cache miss
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from omnibase_core.enums.ticket.enum_dod_check_type import EnumDodCheckType
from omnibase_core.enums.ticket.enum_evidence_kind import EnumEvidenceKind
from omnibase_core.models.contracts.ticket.model_dod_evidence_check import (
    ModelDodEvidenceCheck,
)
from omnibase_core.models.ticket.model_contract_dod_item import ModelContractDodItem
from omnibase_core.models.ticket.model_evidence_requirement import (
    ModelEvidenceRequirement,
)
from omnibase_core.models.ticket.model_golden_path import ModelGoldenPath
from omnibase_core.models.ticket.model_golden_path_input import ModelGoldenPathInput
from omnibase_core.models.ticket.model_golden_path_output import ModelGoldenPathOutput
from omnibase_core.models.ticket.model_requirement import ModelRequirement
from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract

from omnimarket.nodes.node_pipeline_cache_effect.handlers.handler_pipeline_cache import (
    HandlerPipelineCache,
)
from omnimarket.nodes.node_pipeline_cache_effect.models.model_pipeline_cache_request import (
    ModelPipelineCacheRequest,
)

NODE_DIR = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pipeline_cache_effect"
)
CONTRACT_PATH = NODE_DIR / "contract.yaml"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _contract(ticket_id: str = "OMN-11697") -> ModelTicketContract:
    contract = ModelTicketContract(
        ticket_id=ticket_id,
        title="Context Pack Pipeline Test",
        requirements=[
            ModelRequirement(
                id="req-1",
                statement="Generate artifacts.",
                acceptance=[{"id": "ac-1", "statement": "Tests pass."}],
            )
        ],
        golden_path=ModelGoldenPath(
            input=ModelGoldenPathInput(
                topic="onex.cmd.omnimarket.test-requested.v1",
                fixture="fixtures/test.json",
            ),
            output=ModelGoldenPathOutput(
                topic="onex.evt.omnimarket.test-completed.v1",
            ),
        ),
        dod_evidence=[
            ModelContractDodItem(
                id="dod-1",
                description="Tests pass.",
                checks=[
                    ModelDodEvidenceCheck(
                        check_type=EnumDodCheckType.TEST_PASSES,
                        check_value="uv run pytest",
                    )
                ],
            )
        ],
        evidence_requirements=[
            ModelEvidenceRequirement(
                kind=EnumEvidenceKind.TESTS,
                description="Pytest output captured.",
            )
        ],
    )
    contract.update_fingerprint()
    return contract


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contract_yaml_is_well_formed() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    assert data["name"] == "node_pipeline_cache_effect"
    assert data["node_type"] == "effect"
    assert isinstance(data["contract_version"], dict)
    assert data["contract_version"]["major"] == 1


@pytest.mark.unit
def test_contract_declares_expected_topics() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    bus = data["event_bus"]
    assert "onex.cmd.omnimarket.pipeline-cache-requested.v1" in bus["subscribe_topics"]
    assert "onex.evt.omnimarket.pipeline-cache-completed.v1" in bus["publish_topics"]


@pytest.mark.unit
def test_contract_terminal_event_matches_publish_topic() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    terminal = data["terminal_event"]
    assert terminal in data["event_bus"]["publish_topics"]


@pytest.mark.unit
def test_contract_descriptor_is_effect() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    desc = data["descriptor"]
    assert desc["node_archetype"] == "effect"
    assert desc["purity"] == "effectful"


# ---------------------------------------------------------------------------
# Handler tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cache_miss_runs_compute_and_writes_cache(tmp_path: Path) -> None:
    handler = HandlerPipelineCache()
    request = ModelPipelineCacheRequest(
        contract=_contract(),
        cache_root=str(tmp_path),
    )

    result = handler.handle(request)

    assert result.cache_hit is False
    assert result.cache_key
    assert result.test_generation_result is not None
    assert result.chain_generation_result is not None

    # Cache files must exist after a miss
    key_dir = tmp_path / result.cache_key
    assert (key_dir / "test_generation_result.json").exists()
    assert (key_dir / "chain_generation_result.json").exists()


@pytest.mark.unit
def test_second_run_is_cache_hit(tmp_path: Path) -> None:
    handler = HandlerPipelineCache()
    request = ModelPipelineCacheRequest(
        contract=_contract(),
        cache_root=str(tmp_path),
    )

    first = handler.handle(request)
    second = handler.handle(request)

    assert first.cache_hit is False
    assert second.cache_hit is True
    # Byte-identical output regardless of hit/miss
    assert (
        first.test_generation_result.contract_hash
        == second.test_generation_result.contract_hash
    )
    assert (
        first.chain_generation_result.chain_hash
        == second.chain_generation_result.chain_hash
    )


@pytest.mark.unit
def test_cache_hit_output_identical_to_miss_output(tmp_path: Path) -> None:
    handler = HandlerPipelineCache()
    request = ModelPipelineCacheRequest(
        contract=_contract(),
        cache_root=str(tmp_path),
    )

    miss_result = handler.handle(request)
    hit_result = handler.handle(request)

    # All compute fields must be identical between hit and miss
    assert (
        miss_result.test_generation_result.model_dump()
        == hit_result.test_generation_result.model_dump()
    )
    assert (
        miss_result.chain_generation_result.model_dump()
        == hit_result.chain_generation_result.model_dump()
    )


@pytest.mark.unit
def test_cache_key_is_deterministic_for_same_inputs(tmp_path: Path) -> None:
    handler = HandlerPipelineCache()
    contract = _contract()
    request_a = ModelPipelineCacheRequest(
        contract=contract,
        cache_root=str(tmp_path),
    )
    request_b = ModelPipelineCacheRequest(
        contract=contract,
        cache_root=str(tmp_path / "other"),
    )

    result_a = handler.handle(request_a)
    result_b = handler.handle(request_b)

    assert result_a.cache_key == result_b.cache_key


@pytest.mark.unit
def test_different_contracts_produce_different_cache_keys(tmp_path: Path) -> None:
    handler = HandlerPipelineCache()

    result_a = handler.handle(
        ModelPipelineCacheRequest(
            contract=_contract("OMN-1111"), cache_root=str(tmp_path)
        )
    )
    result_b = handler.handle(
        ModelPipelineCacheRequest(
            contract=_contract("OMN-2222"), cache_root=str(tmp_path)
        )
    )

    assert result_a.cache_key != result_b.cache_key


@pytest.mark.unit
def test_different_generator_versions_produce_different_cache_keys(
    tmp_path: Path,
) -> None:
    handler = HandlerPipelineCache()
    contract = _contract()

    result_a = handler.handle(
        ModelPipelineCacheRequest(
            contract=contract, generator_version="1.0.0", cache_root=str(tmp_path)
        )
    )
    result_b = handler.handle(
        ModelPipelineCacheRequest(
            contract=contract, generator_version="2.0.0", cache_root=str(tmp_path)
        )
    )

    assert result_a.cache_key != result_b.cache_key


@pytest.mark.unit
def test_corrupt_cache_entry_treated_as_miss(tmp_path: Path) -> None:
    handler = HandlerPipelineCache()
    request = ModelPipelineCacheRequest(
        contract=_contract(),
        cache_root=str(tmp_path),
    )

    # Populate with a valid miss first
    first = handler.handle(request)
    assert first.cache_hit is False

    # Corrupt one of the two cache files
    key_dir = tmp_path / first.cache_key
    (key_dir / "test_generation_result.json").write_text("NOT_VALID_JSON{{{")

    # Now should miss again (one entry corrupt)
    second = handler.handle(request)
    assert second.cache_hit is False


@pytest.mark.unit
def test_compute_nodes_not_called_on_cache_hit(tmp_path: Path) -> None:
    mock_test_gen = MagicMock()
    mock_chain_gen = MagicMock()

    # Seed the cache with a real first run
    real_handler = HandlerPipelineCache()
    request = ModelPipelineCacheRequest(
        contract=_contract(),
        cache_root=str(tmp_path),
    )
    real_handler.handle(request)

    # Second run uses mock handlers — they must not be called on hit
    hit_handler = HandlerPipelineCache(
        test_generator=mock_test_gen,
        chain_generator=mock_chain_gen,
    )
    result = hit_handler.handle(request)

    assert result.cache_hit is True
    mock_test_gen.handle.assert_not_called()
    mock_chain_gen.handle.assert_not_called()


@pytest.mark.unit
def test_result_model_is_immutable(tmp_path: Path) -> None:
    handler = HandlerPipelineCache()
    result = handler.handle(
        ModelPipelineCacheRequest(contract=_contract(), cache_root=str(tmp_path))
    )

    import pydantic

    with pytest.raises((pydantic.ValidationError, TypeError)):
        result.cache_hit = True  # type: ignore[misc]
