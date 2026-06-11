# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wiring test: resolver output drives the ROI runner's context assembly.

Proves the resolved artifact_content_map, fed into the runner's
ModelContextRoiRunRequest, makes _assemble_context_text emit REAL per-factor
text (never the [stub content for ...] placeholder) for every canonical ON arm.
This is the seam OMN-12948 closes for OMN-12798.
"""

from __future__ import annotations

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_provenance import (
    EnumContextPackProvenance,
)

from omnimarket.nodes.node_context_artifact_resolver_compute.handlers.handler_artifact_resolver import (
    HandlerArtifactResolver,
)
from omnimarket.nodes.node_context_artifact_resolver_compute.models.model_artifact_resolver_request import (
    ModelArtifactResolverRequest,
)
from omnimarket.nodes.node_context_artifact_resolver_compute.models.model_artifact_resolver_result import (
    EnumArtifactResolverStatus,
)
from omnimarket.nodes.node_context_artifact_resolver_compute.models.model_artifact_source import (
    ModelArtifactSource,
)
from omnimarket.nodes.node_context_roi_compute.models.model_factor_arm import (
    EnumArmLabel,
)
from omnimarket.nodes.node_context_roi_compute.models.model_factor_matrix import (
    build_canonical_factor_matrix,
)
from omnimarket.nodes.node_context_roi_runner.handlers.handler_context_roi_runner import (
    _assemble_context_text,
)

_GENERATED_AT = "2026-06-11T00:00:00+00:00"
_CONTRACT_HASH = "b" * 64

# Per-factor real bodies (small so the union arm fits the budget comfortably).
_FACTOR_BODIES: dict[EnumContextFactor, tuple[str, bool]] = {
    EnumContextFactor.GOLDEN_CHAIN: ("chains:\n  - name: registration", False),
    EnumContextFactor.EXEMPLAR: ("def handle(self): return Output()", False),
    EnumContextFactor.LOCAL_FAILURES: ("AssertionError: topic mismatch", False),
    EnumContextFactor.ARCHITECTURE_PATTERNS: (
        "# Primitives\nCONTRACT, NODE, HANDLER only.",
        True,
    ),
    EnumContextFactor.CLAUDE_MD: ("# Rules\nUse uv. No env defaults.", True),
}


def _resolve_all_factors() -> dict[str, str]:
    sources = tuple(
        ModelArtifactSource(
            factor=factor,
            source_name=f"{factor.value}.src",
            raw_content=body,
            provenance=EnumContextPackProvenance.CURATED,
            source_contract_hash=_CONTRACT_HASH,
            is_markdown_sectioned=sectioned,
        )
        for factor, (body, sectioned) in _FACTOR_BODIES.items()
    )
    result = HandlerArtifactResolver().handle(
        ModelArtifactResolverRequest(
            contract_hash=_CONTRACT_HASH,
            generated_at=_GENERATED_AT,
            sources=sources,
        )
    )
    assert result.status is EnumArtifactResolverStatus.OK
    return result.artifact_content_map


def test_resolver_populates_every_canonical_factor() -> None:
    content_map = _resolve_all_factors()
    for factor in _FACTOR_BODIES:
        assert factor.value in content_map
        assert content_map[factor.value].strip() != ""
        assert "stub content for" not in content_map[factor.value]


def test_every_on_arm_gets_real_content_no_stub() -> None:
    content_map = _resolve_all_factors()
    matrix = build_canonical_factor_matrix()

    for arm in matrix:
        if arm.label == EnumArmLabel.OFF:
            continue
        factor_subset = tuple(f.value for f in arm.factors)
        text, warnings = _assemble_context_text(
            factor_subset=factor_subset,
            artifact_content_map=content_map,
        )
        # No factor of any ON arm falls back to the stub placeholder.
        assert "[stub content for" not in text, f"arm {arm.label} got stub text"
        # Each factor in the arm contributes a real labelled section.
        for factor in arm.factors:
            assert f"[{factor.value}]" in text
        # No unknown-factor warnings for canonical arms.
        assert not any("unknown factor" in w for w in warnings)


def test_off_arm_assembles_empty() -> None:
    content_map = _resolve_all_factors()
    text, warnings = _assemble_context_text(
        factor_subset=(),
        artifact_content_map=content_map,
    )
    assert text == ""
    assert warnings == []
