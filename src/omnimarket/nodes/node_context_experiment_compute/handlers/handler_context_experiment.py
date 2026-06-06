# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Deterministic context experiment pack assembly handler."""

from __future__ import annotations

import hashlib

from omnibase_core.enums.enum_context_factor import EnumContextFactor

from omnimarket.nodes.node_context_experiment_compute.models.model_context_chunk_extended import (
    ModelContextChunkExtended,
)
from omnimarket.nodes.node_context_experiment_compute.models.model_context_experiment_request import (
    ModelContextExperimentRequest,
)
from omnimarket.nodes.node_context_experiment_compute.models.model_context_experiment_result import (
    ModelContextExperimentResult,
)
from omnimarket.nodes.node_context_experiment_compute.models.model_context_pack_extended import (
    ModelContextPackExtended,
)
from omnimarket.nodes.node_context_experiment_compute.models.model_context_pack_validity_scope import (
    ModelContextPackValidityScope,
)


class HandlerContextExperiment:
    """Assemble ordered context packs for supplied factor subsets."""

    def handle(
        self, request: ModelContextExperimentRequest
    ) -> ModelContextExperimentResult:
        packs: list[ModelContextPackExtended] = []
        warnings: list[str] = []

        for index, factors in enumerate(request.factor_subsets):
            chunks = _select_chunks(request.artifacts, factors)
            missing_factors = tuple(
                factor
                for factor in factors
                if not any(chunk.factor == factor for chunk in chunks)
            )
            if missing_factors:
                missing = ", ".join(factor.value for factor in missing_factors)
                warnings.append(
                    f"factor_subset[{index}] has no artifacts for: {missing}"
                )

            packs.append(
                _build_pack(
                    request=request, index=index, factors=factors, chunks=chunks
                )
            )

        return ModelContextExperimentResult(
            status="ok",
            packs=tuple(packs),
            warnings=tuple(warnings),
        )


def _select_chunks(
    artifacts: tuple[ModelContextChunkExtended, ...],
    factors: tuple[EnumContextFactor, ...],
) -> tuple[ModelContextChunkExtended, ...]:
    ordered: list[ModelContextChunkExtended] = []
    for factor in factors:
        ordered.extend(chunk for chunk in artifacts if chunk.factor == factor)
    return tuple(ordered)


def _build_pack(
    *,
    request: ModelContextExperimentRequest,
    index: int,
    factors: tuple[EnumContextFactor, ...],
    chunks: tuple[ModelContextChunkExtended, ...],
) -> ModelContextPackExtended:
    fingerprint = _hash_parts(
        request.task_id,
        request.model_id,
        str(index),
        *(factor.value for factor in factors),
        *(chunk.chunk_id for chunk in chunks),
    )
    validity = ModelContextPackValidityScope(
        model_id=request.model_id,
        harness_kind=request.harness_kind,
        execution_mode=request.execution_mode,
        task_class=request.task_class,
        topology_class=request.topology_class,
    )

    return ModelContextPackExtended(
        pack_id=f"pack_{fingerprint[:12]}",
        contract_hash=request.contract_hash,
        model_id=request.model_id,
        chunks=chunks,
        total_token_estimate=sum(chunk.token_estimate for chunk in chunks),
        generated_at=request.generated_at,
        profile_version=request.profile_version,
        factor_ordering=factors,
        valid_for=validity,
        profile_hash=_hash_parts(
            *(factor.value for factor in factors), *(chunk.chunk_id for chunk in chunks)
        ),
        generator_hash=_hash_parts(
            "node_context_experiment_compute",
            request.generator_version,
            request.contract_hash,
        ),
        generator_version=request.generator_version,
    )


def _hash_parts(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


__all__ = ["HandlerContextExperiment"]
