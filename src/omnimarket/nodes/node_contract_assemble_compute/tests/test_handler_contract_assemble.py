# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocol tests for HandlerContractAssemble.

Pins the assembly contract: the emitted YAML round-trips through
``yaml.safe_load``, carries the required top-level sections built from the typed
inputs, places each fragment under its declared type key, and stamps the
DO-NOT-EDIT header. Assembly consumes fragments by type, never as free text.
"""

from __future__ import annotations

import pytest
import yaml

from omnimarket.contract_assembly.advanced_features import resolve_advanced_features
from omnimarket.contract_assembly.models import (
    EnumNodeArchetype,
    EnumSubcontractType,
    ModelAdvancedFeaturesRequest,
    ModelContractAssembleRequest,
    ModelContractDraft,
    ModelContractMetadata,
    ModelSemVer,
    ModelSubcontractRenderRequest,
)
from omnimarket.contract_assembly.render import render_subcontract
from omnimarket.nodes.node_contract_assemble_compute.handlers.handler_contract_assemble import (
    HandlerContractAssemble,
)


def _metadata() -> ModelContractMetadata:
    return ModelContractMetadata(
        node_name="NodeFooCompute",
        service_name="foo_compute",
        namespace="omninode.services.foo.compute",
        node_type="COMPUTE",
        version=ModelSemVer(major=1, minor=2, patch=3),
        description="A foo compute node",
        tags=("compute", "foo"),
    )


def _assemble(*types: EnumSubcontractType) -> ModelContractDraft:
    fragments = tuple(
        render_subcontract(ModelSubcontractRenderRequest(type=t)) for t in types
    )
    advanced = resolve_advanced_features(
        ModelAdvancedFeaturesRequest(archetype=EnumNodeArchetype.COMPUTE)
    )
    return HandlerContractAssemble().handle(
        ModelContractAssembleRequest(
            metadata=_metadata(),
            fragments=fragments,
            advanced_features=advanced,
        )
    )


@pytest.mark.unit
class TestContractAssemble:
    def test_emitted_yaml_round_trips_through_safe_load(self) -> None:
        draft = _assemble(EnumSubcontractType.COMPUTE)
        parsed = yaml.safe_load(draft.contract_yaml)
        assert isinstance(parsed, dict)

    def test_required_top_level_sections_present(self) -> None:
        draft = _assemble(EnumSubcontractType.COMPUTE)
        parsed = yaml.safe_load(draft.contract_yaml)
        assert set(parsed.keys()) >= {"metadata", "subcontracts", "advanced_features"}

    def test_metadata_fields_survive_the_round_trip(self) -> None:
        draft = _assemble(EnumSubcontractType.COMPUTE)
        parsed = yaml.safe_load(draft.contract_yaml)
        assert parsed["metadata"]["node_name"] == "NodeFooCompute"
        assert parsed["metadata"]["version"] == {"major": 1, "minor": 2, "patch": 3}

    def test_do_not_edit_header_present(self) -> None:
        draft = _assemble(EnumSubcontractType.COMPUTE)
        assert "DO NOT EDIT" in draft.contract_yaml

    def test_each_fragment_placed_under_its_type_key(self) -> None:
        draft = _assemble(EnumSubcontractType.COMPUTE, EnumSubcontractType.EVENT)
        parsed = yaml.safe_load(draft.contract_yaml)
        assert set(parsed["subcontracts"].keys()) == {"compute", "event"}
        assert "operations" in parsed["subcontracts"]["compute"]

    def test_advanced_features_serialized_from_typed_block(self) -> None:
        draft = _assemble(EnumSubcontractType.COMPUTE)
        parsed = yaml.safe_load(draft.contract_yaml)
        assert parsed["advanced_features"]["circuit_breaker"]["enabled"] is False

    def test_assembly_is_deterministic(self) -> None:
        first = _assemble(EnumSubcontractType.DATABASE)
        second = _assemble(EnumSubcontractType.DATABASE)
        assert first.contract_yaml == second.contract_yaml
