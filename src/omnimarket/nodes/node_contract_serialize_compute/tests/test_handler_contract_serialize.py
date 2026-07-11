# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocol tests for HandlerContractSerialize — end-to-end composition.

Pins the keystone contract: the serialized document parses, its digest matches an
independent digest of the emitted YAML, the rendered fragments echo the selected
subcontracts, the archetype flows through to the advanced-features block, the lint
gate passes on a well-formed document, and the whole pipeline is deterministic.
"""

from __future__ import annotations

import hashlib

import pytest
import yaml

from omnimarket.contract_assembly.lint import lint_contract
from omnimarket.contract_assembly.models import (
    EnumLintStatus,
    EnumNodeArchetype,
    EnumSubcontractType,
    ModelContractAssemblyRequest,
    ModelContractDocument,
    ModelNodeAnalysis,
    ModelSubcontractSelection,
)
from omnimarket.nodes.node_contract_serialize_compute.handlers.handler_contract_serialize import (
    HandlerContractSerialize,
)


def _serialize(
    archetype: EnumNodeArchetype = EnumNodeArchetype.COMPUTE,
    *types: EnumSubcontractType,
) -> ModelContractDocument:
    selections = tuple(
        ModelSubcontractSelection(type=t)
        for t in (types or (EnumSubcontractType.COMPUTE, EnumSubcontractType.EVENT))
    )
    return HandlerContractSerialize().handle(
        ModelContractAssemblyRequest(
            node_name="NodeFooCompute",
            namespace="omninode.services.foo.compute",
            archetype=archetype,
            analysis=ModelNodeAnalysis(description="A foo node", tags=("compute",)),
            subcontract_selections=selections,
        )
    )


@pytest.mark.unit
class TestContractSerialize:
    def test_document_parses_as_yaml_mapping(self) -> None:
        doc = _serialize()
        parsed = yaml.safe_load(doc.contract_yaml)
        assert set(parsed.keys()) >= {"metadata", "subcontracts", "advanced_features"}

    def test_digest_matches_independent_hash_of_contract_yaml(self) -> None:
        doc = _serialize()
        expected = hashlib.sha256(doc.contract_yaml.encode("utf-8")).hexdigest()
        assert doc.contract_sha256 == expected

    def test_rendered_fragments_echo_the_selected_subcontracts(self) -> None:
        doc = _serialize(
            EnumNodeArchetype.COMPUTE,
            EnumSubcontractType.DATABASE,
            EnumSubcontractType.API,
        )
        rendered = {fragment.type for fragment in doc.subcontracts_rendered}
        assert rendered == {EnumSubcontractType.DATABASE, EnumSubcontractType.API}

    def test_archetype_flows_into_advanced_features(self) -> None:
        compute_doc = _serialize(EnumNodeArchetype.COMPUTE)
        effect_doc = _serialize(EnumNodeArchetype.EFFECT)
        compute_af = yaml.safe_load(compute_doc.contract_yaml)["advanced_features"]
        effect_af = yaml.safe_load(effect_doc.contract_yaml)["advanced_features"]
        assert compute_af["circuit_breaker"]["enabled"] is False
        assert effect_af["circuit_breaker"]["enabled"] is True

    def test_lint_passes_on_well_formed_document(self) -> None:
        doc = _serialize()
        assert doc.lint_status is EnumLintStatus.PASS
        assert doc.lint_messages == ()

    def test_service_name_derived_from_node_name(self) -> None:
        doc = _serialize()
        parsed = yaml.safe_load(doc.contract_yaml)
        assert parsed["metadata"]["service_name"] == "foo_compute"

    def test_do_not_edit_header_present(self) -> None:
        doc = _serialize()
        assert "DO NOT EDIT" in doc.contract_yaml

    def test_pipeline_is_deterministic(self) -> None:
        first = _serialize()
        second = _serialize()
        assert first.contract_sha256 == second.contract_sha256
        assert first.contract_yaml == second.contract_yaml

    def test_empty_selection_still_produces_a_valid_document(self) -> None:
        doc = HandlerContractSerialize().handle(
            ModelContractAssemblyRequest(
                node_name="NodeBareCompute",
                namespace="omninode.services.bare.compute",
                archetype=EnumNodeArchetype.COMPUTE,
            )
        )
        parsed = yaml.safe_load(doc.contract_yaml)
        assert parsed["subcontracts"] == {}
        assert doc.lint_status is EnumLintStatus.PASS

    @pytest.mark.parametrize(
        ("contract_yaml", "message"),
        [
            (
                "metadata: null\nsubcontracts: {}\nadvanced_features: {}\n",
                "metadata section is not a mapping",
            ),
            (
                "metadata: {}\nsubcontracts: {}\nadvanced_features: []\n",
                "advanced_features section is not a mapping",
            ),
            (
                "metadata: {}\nsubcontracts:\n  compute:\n    operations: null\nadvanced_features: {}\n",
                "subcontract 'compute' declares no operations",
            ),
        ],
    )
    def test_lint_rejects_malformed_section_types(
        self, contract_yaml: str, message: str
    ) -> None:
        result = lint_contract(contract_yaml)
        assert result.status is EnumLintStatus.FAIL
        assert message in result.messages
