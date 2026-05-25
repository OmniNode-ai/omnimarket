# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Schema tests for ModelContextPackExtended (OMN-12034).

Acceptance criteria:
- Ordered chunks (tuple, not list) with factor_ordering respected
- valid_for scope, profile_hash, generator_hash fields present and enforced
- Frozen model: immutable after construction
- Extra fields forbidden
"""

from __future__ import annotations

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_provenance import EnumContextPackProvenance
from pydantic import ValidationError

from omnimarket.nodes.node_context_experiment_compute.models import (
    ModelContextChunkExtended,
    ModelContextPackExtended,
    ModelContextPackValidityScope,
    compute_chunk_id,
)

_FACTOR_ORDERING = (
    EnumContextFactor.GOLDEN_CHAIN,
    EnumContextFactor.EXEMPLAR,
    EnumContextFactor.LOCAL_FAILURES,
    EnumContextFactor.ARCHITECTURE_PATTERNS,
    EnumContextFactor.CLAUDE_MD,
)

_VALID_FOR = ModelContextPackValidityScope(
    model_id="glm-4-5",
    harness_kind="run_transfer_evidence_probe",
    execution_mode="batch",
    task_class="compute_node_generation",
    topology_class="topology_affecting",
)


def _make_chunk(factor: EnumContextFactor, content: str) -> ModelContextChunkExtended:
    return ModelContextChunkExtended(
        chunk_id=compute_chunk_id(factor, content),
        factor=factor,
        content=content,
        token_estimate=len(content) // 4,
        token_estimation_method="heuristic_chars",
        tokenizer_source="internal",
        tokenizer_version="0.1.0",
        estimation_accuracy="estimated",
        provenance=EnumContextPackProvenance.CURATED,
        source_artifact_hash="a" * 64,
        source_ticket_id="OMN-12033",
        source_contract_hash="b" * 64,
        source_run_id=None,
        verifier_status=None,
    )


def _make_pack(
    chunks: tuple[ModelContextChunkExtended, ...],
) -> ModelContextPackExtended:
    return ModelContextPackExtended(
        pack_id="pack_test_001",
        contract_hash="c" * 64,
        model_id="glm-4-5",
        chunks=chunks,
        total_token_estimate=sum(c.token_estimate for c in chunks),
        generated_at="2026-05-25T12:00:00+00:00",
        profile_version="1.0.0",
        factor_ordering=_FACTOR_ORDERING,
        valid_for=_VALID_FOR,
        profile_hash="d" * 64,
        generator_hash="e" * 64,
        generator_version="1.0.0",
    )


class TestModelContextPackValidityScope:
    def test_construction(self) -> None:
        scope = _VALID_FOR
        assert scope.model_id == "glm-4-5"
        assert scope.task_class == "compute_node_generation"

    def test_frozen(self) -> None:
        scope = _VALID_FOR
        with pytest.raises(ValidationError):
            scope.model_id = "other-model"  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ModelContextPackValidityScope(
                model_id="m",
                harness_kind="h",
                execution_mode="e",
                task_class="t",
                topology_class="top",
                unexpected="field",  # type: ignore[call-arg]
            )


class TestModelContextPackExtended:
    def test_construction_single_chunk(self) -> None:
        chunk = _make_chunk(EnumContextFactor.GOLDEN_CHAIN, "golden chain content")
        pack = _make_pack((chunk,))
        assert pack.model_id == "glm-4-5"
        assert len(pack.chunks) == 1
        assert pack.chunks[0].factor == EnumContextFactor.GOLDEN_CHAIN

    def test_chunks_is_tuple(self) -> None:
        chunk = _make_chunk(EnumContextFactor.GOLDEN_CHAIN, "golden chain content")
        pack = _make_pack((chunk,))
        assert isinstance(pack.chunks, tuple)

    def test_factor_ordering_tuple(self) -> None:
        chunk = _make_chunk(EnumContextFactor.GOLDEN_CHAIN, "content")
        pack = _make_pack((chunk,))
        assert isinstance(pack.factor_ordering, tuple)
        assert pack.factor_ordering[0] == EnumContextFactor.GOLDEN_CHAIN

    def test_factor_ordering_precedence(self) -> None:
        # golden_chain must appear before exemplar per research doc §2.3
        golden_idx = _FACTOR_ORDERING.index(EnumContextFactor.GOLDEN_CHAIN)
        exemplar_idx = _FACTOR_ORDERING.index(EnumContextFactor.EXEMPLAR)
        local_idx = _FACTOR_ORDERING.index(EnumContextFactor.LOCAL_FAILURES)
        assert golden_idx < exemplar_idx < local_idx

    def test_valid_for_scope_embedded(self) -> None:
        chunk = _make_chunk(EnumContextFactor.GOLDEN_CHAIN, "content")
        pack = _make_pack((chunk,))
        assert pack.valid_for.model_id == "glm-4-5"
        assert pack.valid_for.topology_class == "topology_affecting"

    def test_provenance_hashes_present(self) -> None:
        chunk = _make_chunk(EnumContextFactor.GOLDEN_CHAIN, "content")
        pack = _make_pack((chunk,))
        assert len(pack.profile_hash) > 0
        assert len(pack.generator_hash) > 0
        assert pack.generator_version == "1.0.0"

    def test_frozen(self) -> None:
        chunk = _make_chunk(EnumContextFactor.GOLDEN_CHAIN, "content")
        pack = _make_pack((chunk,))
        with pytest.raises(ValidationError):
            pack.model_id = "mutated"  # type: ignore[misc]

    def test_multi_factor_ordered_chunks(self) -> None:
        golden = _make_chunk(EnumContextFactor.GOLDEN_CHAIN, "golden chain example")
        exemplar = _make_chunk(EnumContextFactor.EXEMPLAR, "working example handler")
        failures = _make_chunk(
            EnumContextFactor.LOCAL_FAILURES, "failure: hardcoded name"
        )
        pack = _make_pack((golden, exemplar, failures))
        assert pack.chunks[0].factor == EnumContextFactor.GOLDEN_CHAIN
        assert pack.chunks[1].factor == EnumContextFactor.EXEMPLAR
        assert pack.chunks[2].factor == EnumContextFactor.LOCAL_FAILURES

    def test_total_token_estimate(self) -> None:
        golden = _make_chunk(EnumContextFactor.GOLDEN_CHAIN, "golden chain example")
        exemplar = _make_chunk(EnumContextFactor.EXEMPLAR, "working example handler")
        pack = _make_pack((golden, exemplar))
        assert (
            pack.total_token_estimate == golden.token_estimate + exemplar.token_estimate
        )
