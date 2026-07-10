# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocol tests for HandlerSubcontractRender — one golden fragment per type.

The render is a single discriminated union over the subcontract type; these tests
pin its I/O contract: every type renders a parseable fragment keyed by its own
type, the digest matches the rendered text, operations override and canonical
fallback both work, and an unknown type is rejected at the boundary.
"""

from __future__ import annotations

import hashlib

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.contract_assembly.models import (
    EnumSubcontractType,
    ModelSubcontractFragment,
    ModelSubcontractRenderRequest,
)
from omnimarket.contract_assembly.render import canonical_operations
from omnimarket.nodes.node_subcontract_render_compute.handlers.handler_subcontract_render import (
    HandlerSubcontractRender,
)


def _render(
    subcontract_type: EnumSubcontractType,
    *,
    operations: tuple[str, ...] = (),
    extra_fields: dict[str, str] | None = None,
) -> ModelSubcontractFragment:
    return HandlerSubcontractRender().handle(
        ModelSubcontractRenderRequest(
            type=subcontract_type,
            operations=operations,
            extra_fields=extra_fields or {},
        )
    )


@pytest.mark.unit
class TestSubcontractRenderGoldenPerType:
    @pytest.mark.parametrize("subcontract_type", list(EnumSubcontractType))
    def test_every_type_renders_a_fragment_keyed_by_its_type(
        self, subcontract_type: EnumSubcontractType
    ) -> None:
        fragment = _render(subcontract_type)
        parsed = yaml.safe_load(fragment.yaml_fragment)
        # Fragment is keyed by the declared type — the boundary the assembler reads.
        assert list(parsed.keys()) == [subcontract_type.value]
        assert parsed[subcontract_type.value]["operations"] == list(
            canonical_operations(subcontract_type)
        )

    @pytest.mark.parametrize("subcontract_type", list(EnumSubcontractType))
    def test_type_is_echoed_on_the_fragment(
        self, subcontract_type: EnumSubcontractType
    ) -> None:
        fragment = _render(subcontract_type)
        assert fragment.type is subcontract_type

    def test_sha256_matches_rendered_yaml_fragment(self) -> None:
        fragment = _render(EnumSubcontractType.COMPUTE)
        expected = hashlib.sha256(fragment.yaml_fragment.encode("utf-8")).hexdigest()
        assert fragment.sha256 == expected

    def test_operations_override_replaces_canonical_list(self) -> None:
        fragment = _render(EnumSubcontractType.COMPUTE, operations=("only_one",))
        parsed = yaml.safe_load(fragment.yaml_fragment)
        assert parsed["compute"]["operations"] == ["only_one"]

    def test_extra_fields_are_merged_into_the_fragment_body(self) -> None:
        fragment = _render(
            EnumSubcontractType.EVENT, extra_fields={"transport": "kafka"}
        )
        parsed = yaml.safe_load(fragment.yaml_fragment)
        assert parsed["event"]["transport"] == "kafka"

    def test_render_is_deterministic(self) -> None:
        first = _render(EnumSubcontractType.DATABASE)
        second = _render(EnumSubcontractType.DATABASE)
        assert first.sha256 == second.sha256
        assert first.yaml_fragment == second.yaml_fragment

    def test_distinct_types_produce_distinct_digests(self) -> None:
        api = _render(EnumSubcontractType.API)
        state = _render(EnumSubcontractType.STATE)
        assert api.sha256 != state.sha256


@pytest.mark.unit
class TestSubcontractRenderRejectsUnknownType:
    def test_unknown_type_is_rejected_at_the_boundary(self) -> None:
        with pytest.raises(ValidationError):
            ModelSubcontractRenderRequest(type="not_a_real_type")
