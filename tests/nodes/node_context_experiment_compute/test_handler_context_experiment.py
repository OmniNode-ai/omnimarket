# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Handler tests for node_context_experiment_compute."""

from __future__ import annotations

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_provenance import EnumContextPackProvenance

from omnimarket.nodes.node_context_experiment_compute.handlers import (
    HandlerContextExperiment,
)
from omnimarket.nodes.node_context_experiment_compute.models import (
    ModelContextChunkExtended,
    ModelContextExperimentRequest,
    ModelContextExperimentResult,
    compute_chunk_id,
)


def _make_chunk(factor: EnumContextFactor, content: str) -> ModelContextChunkExtended:
    return ModelContextChunkExtended(
        chunk_id=compute_chunk_id(factor, content),
        factor=factor,
        content=content,
        token_estimate=max(1, len(content) // 4),
        token_estimation_method="heuristic_chars",
        tokenizer_source="internal",
        tokenizer_version="0.1.0",
        estimation_accuracy="estimated",
        provenance=EnumContextPackProvenance.CURATED,
        source_artifact_hash="a" * 64,
        source_ticket_id="OMN-12245",
        source_contract_hash="b" * 64,
        source_run_id="test-run",
        verifier_status=None,
    )


def test_contract_referenced_models_are_importable() -> None:
    assert ModelContextExperimentRequest.__name__ == "ModelContextExperimentRequest"
    assert ModelContextExperimentResult.__name__ == "ModelContextExperimentResult"
    assert HandlerContextExperiment.__name__ == "HandlerContextExperiment"


def test_handler_builds_ordered_pack_per_factor_subset() -> None:
    golden = _make_chunk(EnumContextFactor.GOLDEN_CHAIN, "golden chain")
    exemplar = _make_chunk(EnumContextFactor.EXEMPLAR, "example handler")
    local_failure = _make_chunk(EnumContextFactor.LOCAL_FAILURES, "local failure")
    request = ModelContextExperimentRequest(
        task_id="OMN-12245",
        model_id="glm-4-5",
        factor_subsets=(
            (EnumContextFactor.EXEMPLAR, EnumContextFactor.GOLDEN_CHAIN),
            (EnumContextFactor.LOCAL_FAILURES,),
        ),
        artifacts=(golden, exemplar, local_failure),
        contract_hash="c" * 64,
        generated_at="2026-06-06T12:00:00+00:00",
    )

    result = HandlerContextExperiment().handle(request)

    assert result.status == "ok"
    assert result.packs is not None
    assert len(result.packs) == 2
    assert tuple(chunk.factor for chunk in result.packs[0].chunks) == (
        EnumContextFactor.EXEMPLAR,
        EnumContextFactor.GOLDEN_CHAIN,
    )
    assert result.packs[0].total_token_estimate == (
        exemplar.token_estimate + golden.token_estimate
    )
    assert result.packs[0].valid_for.model_id == "glm-4-5"
    assert result.packs[0].pack_id.startswith("pack_")
    assert len(result.packs[0].profile_hash) == 64


def test_handler_reports_missing_subset_artifacts_without_failing() -> None:
    golden = _make_chunk(EnumContextFactor.GOLDEN_CHAIN, "golden chain")
    request = ModelContextExperimentRequest(
        task_id="OMN-12245",
        model_id="glm-4-5",
        factor_subsets=((EnumContextFactor.CLAUDE_MD,),),
        artifacts=(golden,),
        contract_hash="c" * 64,
        generated_at="2026-06-06T12:00:00+00:00",
    )

    result = HandlerContextExperiment().handle(request)

    assert result.status == "ok"
    assert result.packs is not None
    assert result.packs[0].chunks == ()
    assert result.warnings == ("factor_subset[0] has no artifacts for: claude_md",)
