"""Candidate pool scorer — pure, deterministic, no I/O."""

from __future__ import annotations

import json

from omnimarket.nodes.node_candidate_pool_compute.models.model_pool_request import (
    ModelCandidatePoolRequest,
)
from omnimarket.nodes.node_candidate_pool_compute.models.model_pool_result import (
    EnumPoolStatus,
    ModelCandidatePoolResult,
    ModelScoredCandidate,
)


def _count_loc(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip())


def _validate_schema(candidate: str, schema: dict[str, object]) -> bool:
    """Validate candidate JSON string against a JSON Schema dict.

    Uses jsonschema when available; falls back to a structural type-check so
    the node has zero required runtime dependencies beyond stdlib.
    """
    try:
        data = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return False

    try:
        import jsonschema  # type: ignore[import-untyped]

        try:
            jsonschema.validate(instance=data, schema=schema)
            return True
        except jsonschema.ValidationError:
            return False
    except ImportError:
        pass

    # Minimal structural fallback: honour "type" and "required" at root level.
    schema_type = schema.get("type")
    if schema_type == "object" and not isinstance(data, dict):
        return False
    if schema_type == "array" and not isinstance(data, list):
        return False

    required = schema.get("required")
    if (
        isinstance(required, list)
        and isinstance(data, dict)
        and not all(k in data for k in required)
    ):
        return False

    return True


def _fitness(*, schema_valid: bool, loc: int, max_loc: int) -> float:
    """Higher is better. Range [0, 1]."""
    validity_score = 1.0 if schema_valid else 0.0
    # LOC score: 1.0 when loc==0, decays linearly to 0.0 at max_loc, clamped at 0.
    loc_score = max(0.0, 1.0 - loc / max(max_loc, 1))
    # Validity is dominant (weight 0.7); compactness is secondary (weight 0.3).
    return round(0.7 * validity_score + 0.3 * loc_score, 6)


class HandlerCandidatePool:
    """Score and rank candidate outputs. Pure, deterministic, no side effects."""

    def handle(self, request: ModelCandidatePoolRequest) -> ModelCandidatePoolResult:
        if len(request.candidates) < request.min_candidates:
            return ModelCandidatePoolResult(
                status=EnumPoolStatus.ERROR,
                run_id=request.run_id,
                ranked_candidates=[],
                best_candidate_index=-1,
                all_valid=False,
                summary=(
                    f"Insufficient candidates: got {len(request.candidates)}, "
                    f"need {request.min_candidates}"
                ),
                error=(
                    f"need at least {request.min_candidates} candidate(s), "
                    f"got {len(request.candidates)}"
                ),
            )

        scored: list[ModelScoredCandidate] = []
        for idx, candidate in enumerate(request.candidates):
            valid = _validate_schema(candidate, request.target_schema)
            loc = _count_loc(candidate)
            within_budget = loc <= request.max_loc
            scored.append(
                ModelScoredCandidate(
                    original_index=idx,
                    schema_valid=valid,
                    loc=loc,
                    within_budget=within_budget,
                    fitness_score=_fitness(
                        schema_valid=valid, loc=loc, max_loc=request.max_loc
                    ),
                )
            )

        ranked = sorted(scored, key=lambda s: s.fitness_score, reverse=True)
        best_index = ranked[0].original_index
        all_valid = all(s.schema_valid for s in scored)
        valid_count = sum(1 for s in scored if s.schema_valid)
        summary = (
            f"{valid_count}/{len(scored)} candidates schema-valid; "
            f"best at original index {best_index} "
            f"(fitness={ranked[0].fitness_score})"
        )

        return ModelCandidatePoolResult(
            status=EnumPoolStatus.OK,
            run_id=request.run_id,
            ranked_candidates=ranked,
            best_candidate_index=best_index,
            all_valid=all_valid,
            summary=summary,
        )


__all__ = ["HandlerCandidatePool"]
