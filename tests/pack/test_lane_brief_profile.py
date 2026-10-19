# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane_brief profile with the v5 optional tiers (OMN-21133, plan tasks TA.17 and TC.10).

Plan section 4.4 row `lane_brief`: required LANE_RULES, OPERATOR_RULINGS, OPEN_WORK;
optional INVARIANTS, CANONICAL_SHAPES, EXEMPLAR, PLAN_EXCERPTS, CAPABILITY_DIGEST.
Section 4.2 fixes the order the tiers render in.
"""

from __future__ import annotations

from itertools import pairwise

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_failure import EnumContextPackFailure
from omnibase_core.enums.enum_context_pack_provenance import (
    EnumContextPackProvenance,
)

from omnimarket.enums.enum_context_binding_kind import EnumContextBindingKind
from omnimarket.models.context_profile_resolve.model_context_profile_resolve_request import (
    ModelContextProfileResolveRequest,
)
from omnimarket.models.context_profile_resolve.model_context_profile_resolve_result import (
    EnumContextProfileResolveStatus,
)
from omnimarket.nodes.node_context_pack_builder_compute.handlers.handler_context_pack_builder import (
    HandlerContextPackBuilder,
)
from omnimarket.nodes.node_context_profile_resolver_effect.handlers.handler_context_profile_resolver import (
    HandlerContextProfileResolver,
)
from omnimarket.pack.model_context_pack_artifact import ModelContextPackArtifact
from omnimarket.pack.model_context_pack_builder_request import (
    ModelContextPackBuilderRequest,
)
from omnimarket.pack.model_context_pack_builder_result import (
    EnumContextPackBuilderStatus,
)
from omnimarket.pack.model_context_profile import ModelContextProfile

pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures("promoted_context_profile_routing"),
]

F = EnumContextFactor

#: Plan section 4.2, render order of the tiers: rows 1 to 10, with the factors of one
#: row listed in the row's own order. A factor earlier in this tuple renders earlier.
SECTION_4_2_ORDER: tuple[EnumContextFactor, ...] = (
    F.LANE_RULES,
    F.INVARIANTS,
    F.ADRS,
    F.CANONICAL_SHAPES,
    F.ARCHITECTURE_PATTERNS,
    F.EXEMPLAR,
    F.OPERATOR_RULINGS,
    F.OPEN_WORK,
    F.ORCHESTRATION_STATE,
    F.REPO_STATE,
    F.SESSION_GOAL,
    F.CAPABILITY_DIGEST,
    F.PLAN_EXCERPTS,
    F.OPERATOR_WORDS_UNRECORDED,
    F.TRANSCRIPT_EXTRACTS,
    F.CONVERSATION_TAIL,
    F.TAIL_SUMMARY,
)
INLINE_PROFILES = ("lane_brief", "session_resume", "session_start")


def _resolve(name: str) -> ModelContextProfile:
    result = HandlerContextProfileResolver().handle(
        ModelContextProfileResolveRequest(profile_name=name)
    )
    assert result.status is EnumContextProfileResolveStatus.OK, result.errors
    assert result.profile is not None
    return result.profile


def _out_of_order(precedence: tuple[EnumContextFactor, ...]) -> list[str]:
    """Factors that render before a factor section 4.2 puts earlier."""
    rank = {factor: index for index, factor in enumerate(SECTION_4_2_ORDER)}
    unknown = [f.value for f in precedence if f not in rank]
    assert not unknown, f"factors outside the section 4.2 table: {unknown}"
    return [
        later.value
        for earlier, later in pairwise(precedence)
        if rank[later] < rank[earlier]
    ]


def _artifact(
    factor: EnumContextFactor, text: str | None = None
) -> ModelContextPackArtifact:
    content = text or f"{factor.value} quoted whole"
    return ModelContextPackArtifact(
        factor=factor,
        content=content,
        token_estimate=10,
        provenance=EnumContextPackProvenance.OBSERVED,
        source_artifact_hash=f"hash-{factor.value}",
        source_contract_hash="contract-hash",
    )


def _request(
    profile: ModelContextProfile, factors: tuple[EnumContextFactor, ...]
) -> ModelContextPackBuilderRequest:
    return ModelContextPackBuilderRequest(
        contract_hash="contract-hash",
        generated_at="2026-10-19T08:00:00+00:00",
        profile=profile,
        artifacts=tuple(_artifact(factor) for factor in factors),
    )


def test_lane_brief_profile_resolves_with_the_section_4_4_factors() -> None:
    profile = _resolve("lane_brief")
    assert profile.binding_kind is EnumContextBindingKind.INLINE
    assert set(profile.required_factors) == {
        F.LANE_RULES,
        F.OPERATOR_RULINGS,
        F.OPEN_WORK,
    }
    assert set(profile.optional_factors) == {
        F.INVARIANTS,
        F.CANONICAL_SHAPES,
        F.EXEMPLAR,
        F.PLAN_EXCERPTS,
        F.CAPABILITY_DIGEST,
    }
    assert set(profile.factor_precedence) == set(profile.required_factors) | set(
        profile.optional_factors
    )
    assert profile.selection_task_class == "context_selection"


def test_lane_brief_selects_invariants_by_the_lane_scopes_and_shapes_by_archetype() -> (
    None
):
    profile = _resolve("lane_brief")
    assert profile.scopes
    conditional = profile.conditional_factors()
    assert conditional == {F.CANONICAL_SHAPES, F.EXEMPLAR}
    assert {rule.task_kind for rule in profile.task_kind_rules} == {
        "node_compute",
        "node_effect",
        "node_reducer",
        "node_orchestrator",
    }
    assert F.INVARIANTS in profile.unconditional_factors()


@pytest.mark.parametrize("name", INLINE_PROFILES)
def test_every_inline_consumer_profile_places_its_tiers_in_section_4_2_order(
    name: str,
) -> None:
    assert _out_of_order(_resolve(name).factor_precedence) == []


def test_the_order_check_fails_a_tier_out_of_order() -> None:
    """Positive control: the check reports a plan excerpt ahead of the rulings."""
    profile = _resolve("lane_brief")
    swapped = list(profile.factor_precedence)
    i, j = swapped.index(F.OPERATOR_RULINGS), swapped.index(F.PLAN_EXCERPTS)
    swapped[i], swapped[j] = swapped[j], swapped[i]
    assert _out_of_order(tuple(swapped)) != []


def test_lane_brief_stable_tiers_come_first() -> None:
    profile = _resolve("lane_brief")
    stable = set(profile.stable_factors)
    assert stable == {F.LANE_RULES, F.INVARIANTS, F.CANONICAL_SHAPES, F.EXEMPLAR}
    head = profile.factor_precedence[: len(stable)]
    assert set(head) == stable


def test_a_brief_missing_the_cited_ruling_is_refused() -> None:
    """The refuse-on-missing rule of the lane-brief standard survives (TA.17)."""
    profile = _resolve("lane_brief")
    present = tuple(f for f in profile.factor_precedence if f is not F.OPERATOR_RULINGS)
    result = HandlerContextPackBuilder().handle(_request(profile, present))
    assert result.status is EnumContextPackBuilderStatus.FAILED
    assert result.failure_class is EnumContextPackFailure.REQUIRED_FACTOR_MISSING
    assert result.context_pack is None
    assert any("operator_rulings" in error for error in result.errors)


@pytest.mark.parametrize("missing", [F.LANE_RULES, F.OPEN_WORK])
def test_a_brief_missing_any_required_tier_is_refused(
    missing: EnumContextFactor,
) -> None:
    profile = _resolve("lane_brief")
    present = tuple(f for f in profile.factor_precedence if f is not missing)
    result = HandlerContextPackBuilder().handle(_request(profile, present))
    assert result.failure_class is EnumContextPackFailure.REQUIRED_FACTOR_MISSING


def test_a_brief_without_the_optional_tiers_still_renders() -> None:
    profile = _resolve("lane_brief")
    result = HandlerContextPackBuilder().handle(
        _request(profile, tuple(profile.required_factors))
    )
    assert result.status is EnumContextPackBuilderStatus.OK


def test_a_brief_with_every_v5_tier_renders_them_in_section_4_2_order() -> None:
    """Artifacts handed over scrambled still render lane rules first, plans last."""
    profile = _resolve("lane_brief")
    scrambled = tuple(reversed(profile.factor_precedence))
    result = HandlerContextPackBuilder().handle(_request(profile, scrambled))
    assert result.status is EnumContextPackBuilderStatus.OK
    assert result.context_pack is not None
    rendered = tuple(chunk.factor for chunk in result.context_pack.chunks)
    assert _out_of_order(rendered) == []
    assert set(rendered) == set(profile.factor_precedence)
    assert rendered[0] is F.LANE_RULES
    assert rendered[-1] is F.PLAN_EXCERPTS


def test_the_lane_brief_budget_fits_its_declared_window() -> None:
    profile = _resolve("lane_brief")
    effective = profile.effective_window_tokens()
    assert effective is not None
    assert profile.token_budget <= effective
