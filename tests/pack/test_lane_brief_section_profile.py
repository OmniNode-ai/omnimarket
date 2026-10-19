# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The `lane_brief_section` context profile (OMN-21118, plan task TA.17, section 4.4)."""

from __future__ import annotations

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_failure import EnumContextPackFailure
from omnibase_core.enums.enum_context_pack_provenance import EnumContextPackProvenance

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
    ModelContextPackBuilderResult,
)
from omnimarket.pack.model_context_profile import ModelContextProfile

pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures("promoted_context_profile_routing"),
]

F = EnumContextFactor
REQUIRED = (F.LANE_RULES, F.OPERATOR_RULINGS, F.OPEN_WORK)
V5_OPTIONAL_TIERS = {
    F.INVARIANTS,
    F.CANONICAL_SHAPES,
    F.EXEMPLAR,
    F.PLAN_EXCERPTS,
    F.CAPABILITY_DIGEST,
}


def _profile() -> ModelContextProfile:
    result = HandlerContextProfileResolver().handle(
        ModelContextProfileResolveRequest(profile_name="lane_brief_section")
    )
    assert result.status is EnumContextProfileResolveStatus.OK, result.errors
    assert result.profile is not None
    return result.profile


def _artifact(factor: EnumContextFactor) -> ModelContextPackArtifact:
    return ModelContextPackArtifact(
        factor=factor,
        content=f"{factor.value} text",
        token_estimate=10,
        provenance=EnumContextPackProvenance.GENERATED,
        source_artifact_hash=f"sha-{factor.value}",
        source_contract_hash="contract-sha",
    )


def _build(factors: tuple[EnumContextFactor, ...]) -> ModelContextPackBuilderResult:
    return HandlerContextPackBuilder().handle(
        ModelContextPackBuilderRequest(
            contract_hash="contract-sha",
            generated_at="2026-10-19T07:00:00Z",
            profile=_profile(),
            artifacts=tuple(_artifact(factor) for factor in factors),
        )
    )


def test_the_profile_is_the_inline_lane_brief_binding() -> None:
    profile = _profile()
    assert profile.injection_point == "lane_brief"
    assert profile.binding_kind is EnumContextBindingKind.INLINE
    assert set(profile.required_factors) == set(REQUIRED)


def test_the_profile_excludes_the_v5_optional_tiers_for_the_parity_run() -> None:
    profile = _profile()
    assert set(profile.excluded_factors) == V5_OPTIONAL_TIERS
    assert not profile.optional_factors
    assert set(profile.factor_precedence) == set(REQUIRED)


def test_the_profile_needs_no_session() -> None:
    """A lane brief builds without a session (plan section 4.4), so no session tier is named."""
    profile = _profile()
    named = {*profile.required_factors, *profile.optional_factors}
    assert not named & {
        F.CONVERSATION_TAIL,
        F.TRANSCRIPT_EXTRACTS,
        F.TAIL_SUMMARY,
        F.SESSION_GOAL,
    }


def test_the_three_required_factors_build_a_pack() -> None:
    result = _build(REQUIRED)
    assert result.status is EnumContextPackBuilderStatus.OK


@pytest.mark.parametrize("absent", REQUIRED)
def test_a_brief_missing_a_required_factor_is_refused_required_factor_missing(
    absent: EnumContextFactor,
) -> None:
    result = _build(tuple(f for f in REQUIRED if f is not absent))
    assert result.status is EnumContextPackBuilderStatus.FAILED
    assert result.failure_class is EnumContextPackFailure.REQUIRED_FACTOR_MISSING
    assert result.errors == (f"missing required factors: {absent.value}",)
    assert result.context_pack is None
