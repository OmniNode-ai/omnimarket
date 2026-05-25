# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Schema tests for ModelContextChunkExtended and chunk ID stability (OMN-12033).

Acceptance criteria:
- Pydantic frozen model (immutability enforced at runtime)
- ID-stability: same factor + content always produces the same ctx_XXXXXXXX
- Collision detection: different content produces different IDs
"""

from __future__ import annotations

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_provenance import EnumContextPackProvenance
from pydantic import ValidationError

from omnimarket.nodes.node_context_experiment_compute.models import (
    ModelContextChunkExtended,
    compute_chunk_id,
)


def _make_chunk(
    factor: EnumContextFactor = EnumContextFactor.GOLDEN_CHAIN,
    content: str = "golden chain acceptance test content",
    verifier_status: str | None = None,
) -> ModelContextChunkExtended:
    chunk_id = compute_chunk_id(factor, content)
    return ModelContextChunkExtended(
        chunk_id=chunk_id,
        factor=factor,
        content=content,
        token_estimate=len(content) // 4,
        token_estimation_method="heuristic_chars",
        tokenizer_source="internal",
        tokenizer_version="0.1.0",
        estimation_accuracy="estimated",
        provenance=EnumContextPackProvenance.GENERATED,
        source_artifact_hash="a" * 64,
        source_ticket_id=None,
        source_contract_hash="b" * 64,
        source_run_id=None,
        verifier_status=verifier_status,  # type: ignore[arg-type]
    )


class TestComputeChunkId:
    def test_format(self) -> None:
        chunk_id = compute_chunk_id(EnumContextFactor.GOLDEN_CHAIN, "content")
        assert chunk_id.startswith("ctx_")
        assert len(chunk_id) == 12  # "ctx_" + 8 hex chars

    def test_determinism(self) -> None:
        id1 = compute_chunk_id(EnumContextFactor.GOLDEN_CHAIN, "same content")
        id2 = compute_chunk_id(EnumContextFactor.GOLDEN_CHAIN, "same content")
        assert id1 == id2

    def test_factor_affects_id(self) -> None:
        id_golden = compute_chunk_id(EnumContextFactor.GOLDEN_CHAIN, "content")
        id_exemplar = compute_chunk_id(EnumContextFactor.EXEMPLAR, "content")
        assert id_golden != id_exemplar

    def test_content_affects_id(self) -> None:
        id_a = compute_chunk_id(EnumContextFactor.GOLDEN_CHAIN, "content A")
        id_b = compute_chunk_id(EnumContextFactor.GOLDEN_CHAIN, "content B")
        assert id_a != id_b

    def test_known_stable_id(self) -> None:
        # Regression guard: this value must not change across versions.
        # sha256("golden_chain:content")[:8] = 5b9cdf1e
        chunk_id = compute_chunk_id(EnumContextFactor.GOLDEN_CHAIN, "content")
        import hashlib

        expected_hex = hashlib.sha256(b"golden_chain:content").hexdigest()[:8]
        assert chunk_id == f"ctx_{expected_hex}"


class TestModelContextChunkExtended:
    def test_construction(self) -> None:
        chunk = _make_chunk()
        assert chunk.factor == EnumContextFactor.GOLDEN_CHAIN
        assert chunk.verifier_status is None
        assert chunk.chunk_id.startswith("ctx_")

    def test_frozen(self) -> None:
        chunk = _make_chunk()
        with pytest.raises(ValidationError):
            chunk.content = "mutated"  # type: ignore[misc]

    def test_verifier_status_on_local_failures(self) -> None:
        chunk = _make_chunk(
            factor=EnumContextFactor.LOCAL_FAILURES,
            content="failure: hardcoded fixture handler names",
            verifier_status="verified_failure",
        )
        assert chunk.verifier_status == "verified_failure"

    def test_verifier_status_invalid_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_chunk(verifier_status="not_a_valid_status")  # type: ignore[arg-type]

    def test_chunk_id_matches_compute(self) -> None:
        content = "golden chain acceptance test content"
        factor = EnumContextFactor.GOLDEN_CHAIN
        chunk = _make_chunk(factor=factor, content=content)
        assert chunk.chunk_id == compute_chunk_id(factor, content)

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ModelContextChunkExtended(
                chunk_id="ctx_abcd1234",
                factor=EnumContextFactor.GOLDEN_CHAIN,
                content="x",
                token_estimate=0,
                token_estimation_method="heuristic_chars",
                tokenizer_source="internal",
                tokenizer_version="0.1.0",
                estimation_accuracy="estimated",
                provenance=EnumContextPackProvenance.GENERATED,
                source_artifact_hash="a" * 64,
                source_ticket_id=None,
                source_contract_hash="b" * 64,
                source_run_id=None,
                verifier_status=None,
                unknown_field="surprise",  # type: ignore[call-arg]
            )
