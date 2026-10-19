# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane_brief context profile serves the shared pack nodes with data only (OMN-21111)."""

from __future__ import annotations

import hashlib

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

pytestmark = pytest.mark.unit

F = EnumContextFactor


def _resolve() -> ModelContextProfile:
    # The packaged routing contract, with no promoted fixture: the profile names no
    # model-role task class, so it resolves wherever the pack nodes run.
    result = HandlerContextProfileResolver().handle(
        ModelContextProfileResolveRequest(profile_name="lane_brief")
    )
    assert result.status is EnumContextProfileResolveStatus.OK, result.errors
    assert result.profile is not None
    return result.profile


def _artifact(factor: EnumContextFactor, content: str) -> ModelContextPackArtifact:
    return ModelContextPackArtifact(
        factor=factor,
        content=content,
        token_estimate=(len(content) + 3) // 4,
        provenance=EnumContextPackProvenance.CURATED,
        source_artifact_hash=hashlib.sha256(content.encode()).hexdigest(),
        source_contract_hash="c" * 64,
    )


RULE = "### 17. Never override `core.hooksPath`\nNever `git stash` in a worktree."
RULING = '2026-10-14T21:46:25Z | RULING | lane=orchestrator | "fill those slots"'


def test_the_lane_brief_profile_resolves_with_the_packaged_routing_contract() -> None:
    profile = _resolve()
    assert profile.binding_kind is EnumContextBindingKind.INLINE
    assert profile.required_factors == (F.LANE_RULES,)
    assert set(profile.optional_factors) == {F.OPERATOR_RULINGS, F.PLAN_EXCERPTS}
    assert profile.token_budget == 4000
    effective = profile.effective_window_tokens()
    assert effective is not None
    assert effective >= profile.token_budget


def test_the_shared_builder_assembles_a_lane_brief_pack_rules_first() -> None:
    result = HandlerContextPackBuilder().handle(
        ModelContextPackBuilderRequest(
            contract_hash="c" * 64,
            generated_at="2026-10-19T07:36:00Z",
            profile=_resolve(),
            artifacts=(
                _artifact(F.OPERATOR_RULINGS, RULING),
                _artifact(F.LANE_RULES, RULE),
            ),
        )
    )
    assert result.status is EnumContextPackBuilderStatus.OK, result.errors
    assert result.context_pack is not None
    chunks = result.context_pack.chunks
    assert [c.factor for c in chunks] == [F.LANE_RULES, F.OPERATOR_RULINGS]
    assert "Never `git stash` in a worktree." in chunks[0].content
    assert '"fill those slots"' in chunks[1].content


def test_a_lane_brief_pack_without_lane_rules_fails_closed() -> None:
    """Negative control: the required factor missing fails the build, never an empty pack."""
    result = HandlerContextPackBuilder().handle(
        ModelContextPackBuilderRequest(
            contract_hash="c" * 64,
            generated_at="2026-10-19T07:36:00Z",
            profile=_resolve(),
            artifacts=(_artifact(F.OPERATOR_RULINGS, RULING),),
        )
    )
    assert result.status is EnumContextPackBuilderStatus.FAILED
    assert result.failure_class is EnumContextPackFailure.REQUIRED_FACTOR_MISSING
