"""Pure deterministic context-pack assembly with content deduplication.

Optional artifacts that do not fit the token budget are excluded.
"""

from __future__ import annotations

import hashlib
import json

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_failure import EnumContextPackFailure
from omnibase_core.models.pack.model_context_chunk import ModelContextChunk
from omnibase_core.models.pack.model_context_pack import ModelContextPack
from omnibase_core.utils.util_context_pack import compute_chunk_id

from omnimarket.pack.model_context_pack_artifact import (
    ModelContextPackArtifact,
)
from omnimarket.pack.model_context_pack_builder_request import (
    ModelContextPackBuilderRequest,
)
from omnimarket.pack.model_context_pack_builder_result import (
    EnumContextPackBuilderStatus,
    ModelContextPackBudgetExclusion,
    ModelContextPackBuilderResult,
    ModelContextPackDroppedDuplicate,
)
from omnimarket.pack.model_context_profile import (
    ModelContextProfile,
)


def _hash_json(payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _failure(
    failure_class: EnumContextPackFailure,
    *errors: str,
) -> ModelContextPackBuilderResult:
    return ModelContextPackBuilderResult(
        status=EnumContextPackBuilderStatus.FAILED,
        failure_class=failure_class,
        errors=tuple(errors),
    )


def _precedence(profile: ModelContextProfile) -> dict[EnumContextFactor, int]:
    ordered = profile.factor_precedence or (
        profile.required_factors + profile.optional_factors
    )
    return {factor: index for index, factor in enumerate(ordered)}


def _ordered_artifacts(
    artifacts: tuple[ModelContextPackArtifact, ...],
    profile: ModelContextProfile,
) -> tuple[ModelContextPackArtifact, ...]:
    excluded = set(profile.excluded_factors)
    precedence = _precedence(profile)
    return tuple(
        sorted(
            (artifact for artifact in artifacts if artifact.factor not in excluded),
            key=lambda artifact: (
                precedence.get(artifact.factor, len(precedence)),
                artifact.source_priority,
                artifact.source_artifact_hash,
                hashlib.sha256(artifact.content.encode("utf-8")).hexdigest(),
            ),
        )
    )


def _missing_required_factors(
    artifacts: tuple[ModelContextPackArtifact, ...],
    profile: ModelContextProfile,
) -> tuple[EnumContextFactor, ...]:
    available = {artifact.factor for artifact in artifacts}
    return tuple(
        factor for factor in profile.required_factors if factor not in available
    )


def _content_sha256(content: str) -> str:
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    normalised = "\n".join(line.rstrip() for line in content.split("\n")).strip()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _deduplicate_artifacts(
    artifacts: tuple[ModelContextPackArtifact, ...],
    profile: ModelContextProfile,
) -> tuple[
    tuple[ModelContextPackArtifact, ...],
    tuple[ModelContextPackDroppedDuplicate, ...],
]:
    required_factors = set(profile.required_factors)
    keyed = tuple(
        (_content_sha256(artifact.content), artifact) for artifact in artifacts
    )
    winners: dict[str, int] = {}
    for index, (content_hash, artifact) in enumerate(keyed):
        winner_index = winners.get(content_hash)
        if winner_index is None or (
            artifact.factor in required_factors
            and keyed[winner_index][1].factor not in required_factors
        ):
            winners[content_hash] = index

    kept: list[ModelContextPackArtifact] = []
    dropped: list[ModelContextPackDroppedDuplicate] = []
    for index, (content_hash, artifact) in enumerate(keyed):
        winner_index = winners[content_hash]
        if index == winner_index:
            kept.append(artifact)
            continue
        winner = keyed[winner_index][1]
        dropped.append(
            ModelContextPackDroppedDuplicate(
                factor=artifact.factor,
                source_artifact_hash=artifact.source_artifact_hash,
                content_sha256=content_hash,
                kept_factor=winner.factor,
                kept_source_artifact_hash=winner.source_artifact_hash,
            )
        )
    return tuple(kept), tuple(dropped)


def _chunks(
    artifacts: tuple[ModelContextPackArtifact, ...],
    profile: ModelContextProfile,
) -> tuple[ModelContextChunk, ...]:
    return tuple(
        ModelContextChunk(
            chunk_id=compute_chunk_id(artifact.factor, artifact.content),
            factor=artifact.factor,
            content=artifact.content,
            token_estimate=artifact.token_estimate,
            token_estimation_method=profile.token_estimation_method,
            tokenizer_source=profile.tokenizer_source,
            tokenizer_version=profile.tokenizer_version,
            estimation_accuracy=profile.estimation_accuracy,
            provenance=artifact.provenance,
            source_artifact_hash=artifact.source_artifact_hash,
            source_ticket_id=artifact.source_ticket_id,
            source_contract_hash=artifact.source_contract_hash,
            source_run_id=artifact.source_run_id,
        )
        for artifact in artifacts
    )


def _duplicate_chunk_ids(chunks: tuple[ModelContextChunk, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for chunk in chunks:
        if chunk.chunk_id in seen:
            duplicates.append(chunk.chunk_id)
        seen.add(chunk.chunk_id)
    return tuple(duplicates)


def _pack_id(
    request: ModelContextPackBuilderRequest,
    chunks: tuple[ModelContextChunk, ...],
) -> str:
    return (
        "pack_"
        + _hash_json(
            {
                "contract_hash": request.contract_hash,
                "model_id": request.profile.model_id,
                "profile_schema_version": request.profile.profile_schema_version,
                "profile_version": request.profile.profile_version,
                "chunks": [chunk.model_dump(mode="json") for chunk in chunks],
            }
        )[:16]
    )


class HandlerContextPackBuilder:
    """Build context packs from resolved artifacts and validated profile input."""

    def handle(
        self,
        request: ModelContextPackBuilderRequest,
    ) -> ModelContextPackBuilderResult:
        missing = _missing_required_factors(request.artifacts, request.profile)
        if missing:
            return _failure(
                EnumContextPackFailure.REQUIRED_FACTOR_MISSING,
                "missing required factors: "
                + ", ".join(factor.value for factor in missing),
            )

        ordered_artifacts = _ordered_artifacts(request.artifacts, request.profile)
        kept_artifacts, dropped_duplicates = _deduplicate_artifacts(
            ordered_artifacts, request.profile
        )
        required_factors = set(request.profile.required_factors)
        required = tuple(
            artifact
            for artifact in kept_artifacts
            if artifact.factor in required_factors
        )
        required_total = sum(artifact.token_estimate for artifact in required)
        if required_total > request.profile.token_budget:
            return _failure(
                EnumContextPackFailure.TOKEN_BUDGET_EXCEEDED,
                (
                    f"token estimate {required_total} of required artifacts exceeds "
                    f"budget {request.profile.token_budget}: "
                    + ", ".join(
                        f"{artifact.factor.value}:{artifact.source_artifact_hash}"
                        for artifact in required
                    )
                ),
            )

        used = required_total
        admitted: list[ModelContextPackArtifact] = []
        excluded: list[ModelContextPackBudgetExclusion] = []
        for artifact in kept_artifacts:
            if artifact.factor in required_factors:
                admitted.append(artifact)
            elif used + artifact.token_estimate <= request.profile.token_budget:
                admitted.append(artifact)
                used += artifact.token_estimate
            else:
                excluded.append(
                    ModelContextPackBudgetExclusion(
                        factor=artifact.factor,
                        source_artifact_hash=artifact.source_artifact_hash,
                        token_estimate=artifact.token_estimate,
                    )
                )

        chunks = _chunks(tuple(admitted), request.profile)
        duplicate_ids = _duplicate_chunk_ids(chunks)
        if duplicate_ids:
            return _failure(
                EnumContextPackFailure.ARTIFACT_HASH_MISMATCH,
                "duplicate chunk ids: " + ", ".join(duplicate_ids),
            )

        total_tokens = sum(chunk.token_estimate for chunk in chunks)
        context_pack = ModelContextPack(
            pack_id=_pack_id(request, chunks),
            contract_hash=request.contract_hash,
            model_id=request.profile.model_id,
            chunks=chunks,
            total_token_estimate=total_tokens,
            generated_at=request.generated_at,
            profile_version=request.profile.profile_version,
        )
        pack_hash = _hash_json(context_pack.model_dump(mode="json"))
        return ModelContextPackBuilderResult(
            status=EnumContextPackBuilderStatus.OK,
            context_pack=context_pack,
            pack_hash=pack_hash,
            dropped_duplicates=dropped_duplicates,
            excluded_by_budget=tuple(reversed(excluded)),
        )


__all__ = ["HandlerContextPackBuilder"]
