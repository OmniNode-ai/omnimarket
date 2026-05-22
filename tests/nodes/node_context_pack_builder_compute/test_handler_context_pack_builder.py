"""Tests for deterministic context-pack assembly."""

from __future__ import annotations

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_failure import EnumContextPackFailure
from omnibase_core.enums.enum_context_pack_provenance import (
    EnumContextPackProvenance,
)

from omnimarket.nodes.node_context_pack_builder_compute.handlers.handler_context_pack_builder import (
    HandlerContextPackBuilder,
)
from omnimarket.nodes.node_context_pack_builder_compute.models.model_context_pack_artifact import (
    ModelContextPackArtifact,
)
from omnimarket.nodes.node_context_pack_builder_compute.models.model_context_pack_builder_request import (
    ModelContextPackBuilderRequest,
)
from omnimarket.nodes.node_context_pack_builder_compute.models.model_context_pack_builder_result import (
    EnumContextPackBuilderStatus,
)
from omnimarket.nodes.node_context_pack_builder_compute.models.model_context_profile import (
    ModelContextProfile,
)


def _profile(**overrides: object) -> ModelContextProfile:
    values = {
        "model_id": "glm-4.5",
        "required_factors": (EnumContextFactor.GOLDEN_CHAIN,),
        "optional_factors": (
            EnumContextFactor.EXEMPLAR,
            EnumContextFactor.LOCAL_FAILURES,
        ),
        "excluded_factors": (EnumContextFactor.CLAUDE_MD,),
        "factor_precedence": (
            EnumContextFactor.GOLDEN_CHAIN,
            EnumContextFactor.EXEMPLAR,
            EnumContextFactor.LOCAL_FAILURES,
        ),
        "token_budget": 100,
        "profile_version": "2026.05.22",
    }
    values.update(overrides)
    return ModelContextProfile(**values)


def _artifact(
    factor: EnumContextFactor,
    content: str,
    *,
    token_estimate: int = 10,
    source_hash: str | None = None,
    priority: int = 100,
) -> ModelContextPackArtifact:
    return ModelContextPackArtifact(
        factor=factor,
        content=content,
        token_estimate=token_estimate,
        provenance=EnumContextPackProvenance.GENERATED,
        source_artifact_hash=source_hash or f"sha-{factor.value}-{content}",
        source_ticket_id="OMN-11697",
        source_contract_hash="contract-sha",
        source_run_id="run-1",
        source_priority=priority,
    )


def _request(
    artifacts: tuple[ModelContextPackArtifact, ...],
    *,
    profile: ModelContextProfile | None = None,
) -> ModelContextPackBuilderRequest:
    return ModelContextPackBuilderRequest(
        contract_hash="contract-sha",
        generated_at="2026-05-22T19:00:00Z",
        profile=profile or _profile(),
        artifacts=artifacts,
    )


@pytest.mark.unit
class TestHandlerContextPackBuilder:
    def test_builds_pack_with_deterministic_factor_order(self) -> None:
        result = HandlerContextPackBuilder().handle(
            _request(
                (
                    _artifact(EnumContextFactor.EXEMPLAR, "passing exemplar"),
                    _artifact(EnumContextFactor.GOLDEN_CHAIN, "expected chain"),
                    _artifact(EnumContextFactor.CLAUDE_MD, "excluded instructions"),
                )
            )
        )

        assert result.status == EnumContextPackBuilderStatus.OK
        assert result.context_pack is not None
        assert result.context_pack.pack_id.startswith("pack_")
        assert result.context_pack.total_token_estimate == 20
        assert tuple(chunk.factor for chunk in result.context_pack.chunks) == (
            EnumContextFactor.GOLDEN_CHAIN,
            EnumContextFactor.EXEMPLAR,
        )

    def test_same_input_produces_same_pack_identity(self) -> None:
        handler = HandlerContextPackBuilder()
        request = _request((_artifact(EnumContextFactor.GOLDEN_CHAIN, "chain"),))

        first = handler.handle(request)
        second = handler.handle(request)

        assert first.context_pack is not None
        assert second.context_pack is not None
        assert first.context_pack.pack_id == second.context_pack.pack_id
        assert first.pack_hash == second.pack_hash

    def test_missing_required_factor_fails_typed(self) -> None:
        result = HandlerContextPackBuilder().handle(
            _request((_artifact(EnumContextFactor.EXEMPLAR, "exemplar"),))
        )

        assert result.status == EnumContextPackBuilderStatus.FAILED
        assert result.failure_class == EnumContextPackFailure.REQUIRED_FACTOR_MISSING
        assert result.context_pack is None

    def test_token_budget_exceeded_fails_typed(self) -> None:
        result = HandlerContextPackBuilder().handle(
            _request(
                (
                    _artifact(
                        EnumContextFactor.GOLDEN_CHAIN, "chain", token_estimate=101
                    ),
                )
            )
        )

        assert result.status == EnumContextPackBuilderStatus.FAILED
        assert result.failure_class == EnumContextPackFailure.TOKEN_BUDGET_EXCEEDED

    def test_duplicate_chunk_ids_fail_typed(self) -> None:
        result = HandlerContextPackBuilder().handle(
            _request(
                (
                    _artifact(EnumContextFactor.GOLDEN_CHAIN, "same", source_hash="a"),
                    _artifact(EnumContextFactor.GOLDEN_CHAIN, "same", source_hash="b"),
                )
            )
        )

        assert result.status == EnumContextPackBuilderStatus.FAILED
        assert result.failure_class == EnumContextPackFailure.ARTIFACT_HASH_MISMATCH

    def test_invalid_profile_rejects_required_excluded_overlap(self) -> None:
        with pytest.raises(ValueError, match="required_factors"):
            _profile(
                required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
                excluded_factors=(EnumContextFactor.GOLDEN_CHAIN,),
            )
