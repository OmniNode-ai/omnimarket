# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wiring test: resolver artifacts reach the canonical pack builder whole.

The resolver returns ``ModelContextPackArtifact`` entries and applies no
selection, dedup, budget or pack hash (OMN-21137). The ROI runner passes them
to node_context_pack_builder_compute, which alone selects, budgets and hashes,
so every section of a source reaches the builder and the pack hash and any
budget failure are the builder's. OMN-12948 opened this seam for OMN-12798;
OMN-13643 routed it through the single canonical assembler.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_provenance import (
    EnumContextPackProvenance,
)
from pydantic import ValidationError

from omnimarket.events.context_roi import EnumFailureStage
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
from omnimarket.nodes.node_context_pack_builder_compute.handlers.handler_context_pack_builder import (
    HandlerContextPackBuilder,
)
from omnimarket.nodes.node_context_roi_compute.models.model_factor_arm import (
    EnumArmLabel,
)
from omnimarket.nodes.node_context_roi_compute.models.model_factor_matrix import (
    build_canonical_factor_matrix,
)
from omnimarket.nodes.node_context_roi_runner.handlers.handler_context_roi_runner import (
    HandlerContextRoiRunner,
    _build_context_pack,
)
from omnimarket.nodes.node_context_roi_runner.models.model_context_roi_run_request import (
    ModelContextRoiArmSpec,
    ModelContextRoiRunRequest,
    ModelContextRoiTask,
)
from omnimarket.pack import (
    EnumContextPackBuilderStatus,
    ModelContextPackArtifact,
    ModelContextPackBuilderRequest,
    ModelContextProfile,
)

_GENERATED_AT = "2026-06-11T00:00:00+00:00"
_CONTRACT_HASH = "b" * 64
_RUNNER_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_context_roi_runner"
    / "contract.yaml"
)

# Per-factor real bodies (small so the union arm fits the budget comfortably).
_FACTOR_BODIES: dict[EnumContextFactor, tuple[str, bool]] = {
    EnumContextFactor.GOLDEN_CHAIN: ("chains:\n  - name: registration", False),
    EnumContextFactor.EXEMPLAR: ("def handle(self): return Output()", False),
    EnumContextFactor.LOCAL_FAILURES: ("AssertionError: topic mismatch", False),
    EnumContextFactor.ARCHITECTURE_PATTERNS: (
        "# Primitives\nCONTRACT, NODE, HANDLER only.",
        True,
    ),
    EnumContextFactor.CLAUDE_MD: ("# Rules\nUse uv.\n# Env\nNo env defaults.", True),
}


def _source(
    factor: EnumContextFactor, body: str, *, sectioned: bool
) -> ModelArtifactSource:
    return ModelArtifactSource(
        factor=factor,
        source_name=f"{factor.value}.src",
        raw_content=body,
        provenance=EnumContextPackProvenance.CURATED,
        source_contract_hash=_CONTRACT_HASH,
        is_markdown_sectioned=sectioned,
    )


def _resolve(*sources: ModelArtifactSource) -> tuple[ModelContextPackArtifact, ...]:
    result = HandlerArtifactResolver().handle(
        ModelArtifactResolverRequest(
            contract_hash=_CONTRACT_HASH,
            generated_at=_GENERATED_AT,
            sources=sources,
        )
    )
    assert result.status is EnumArtifactResolverStatus.OK
    return result.artifacts


def _resolve_all_factors() -> tuple[ModelContextPackArtifact, ...]:
    return _resolve(
        *(
            _source(factor, body, sectioned=sectioned)
            for factor, (body, sectioned) in _FACTOR_BODIES.items()
        )
    )


def test_resolver_covers_every_canonical_factor() -> None:
    artifacts = _resolve_all_factors()
    assert {a.factor for a in artifacts} == set(_FACTOR_BODIES)
    assert all("stub content for" not in a.content for a in artifacts)


def test_every_on_arm_gets_real_content_no_stub() -> None:
    artifacts = _resolve_all_factors()
    for arm in build_canonical_factor_matrix():
        if arm.label == EnumArmLabel.OFF:
            continue
        factor_subset = tuple(f.value for f in arm.factors)
        text, pack_hash, warnings, failure_stage = _build_context_pack(
            factor_subset=factor_subset,
            artifact_content_map={},
            contract_hash=_CONTRACT_HASH,
            resolved_artifacts=artifacts,
        )
        assert failure_stage == EnumFailureStage.NONE, (
            f"arm {arm.label} pack build failed: {warnings}"
        )
        assert pack_hash != ""
        assert "[stub content for" not in text, f"arm {arm.label} got stub text"
        for factor in arm.factors:
            assert f"[{factor.value}]" in text
        assert not any("unknown factor" in w for w in warnings)


def test_every_section_reaches_the_builder() -> None:
    # Four ~1000-token sections: 4000 tokens, more than the 3000-token
    # per-factor cut the resolver used to apply. All four reach the builder.
    body = "\n".join(f"# Section {i}\n" + ("x" * 3990) for i in range(1, 5))
    artifacts = _resolve(_source(EnumContextFactor.CLAUDE_MD, body, sectioned=True))
    assert len(artifacts) == 4

    text, pack_hash, warnings, failure_stage = _build_context_pack(
        factor_subset=("claude_md",),
        artifact_content_map={},
        contract_hash=_CONTRACT_HASH,
        resolved_artifacts=artifacts,
    )
    assert failure_stage == EnumFailureStage.NONE, warnings
    assert text.count("[claude_md]") == 4
    for i in range(1, 5):
        assert f"# Section {i}" in text
    assert pack_hash != ""


def test_wire_decoded_artifacts_match_arm_factor_labels() -> None:
    # Artifacts arrive on the runner request as JSON; the factor is the string
    # label on the wire and an enum member after validation. Each arm label
    # selects exactly the artifacts of that factor.
    wire = [
        {
            "factor": label,
            "content": f"{label} body",
            "token_estimate": 3,
            "provenance": EnumContextPackProvenance.OBSERVED.value,
            "source_artifact_hash": f"hash-{label}",
            "source_contract_hash": _CONTRACT_HASH,
        }
        for label in ("claude_md", "golden_chain", "exemplar")
    ]
    decoded = tuple(
        ModelContextPackArtifact.model_validate_json(json.dumps(item)) for item in wire
    )
    text, _pack_hash, warnings, failure_stage = _build_context_pack(
        factor_subset=("claude_md", "exemplar"),
        artifact_content_map={},
        contract_hash=_CONTRACT_HASH,
        resolved_artifacts=decoded,
    )
    assert failure_stage == EnumFailureStage.NONE, warnings
    assert "[claude_md]\nclaude_md body" in text
    assert "[exemplar]\nexemplar body" in text
    assert "golden_chain body" not in text


def test_pack_hash_is_the_builders() -> None:
    artifacts = _resolve(
        _source(EnumContextFactor.GOLDEN_CHAIN, "chain text", sectioned=False)
    )
    _text, pack_hash, _warnings, failure_stage = _build_context_pack(
        factor_subset=("golden_chain",),
        artifact_content_map={},
        contract_hash=_CONTRACT_HASH,
        resolved_artifacts=artifacts,
    )
    assert failure_stage == EnumFailureStage.NONE
    direct = HandlerContextPackBuilder().handle(
        ModelContextPackBuilderRequest(
            contract_hash=_CONTRACT_HASH,
            generated_at=_GENERATED_AT,
            profile=ModelContextProfile(
                model_id="context-roi-experiment",
                factor_precedence=tuple(EnumContextFactor),
            ),
            artifacts=artifacts,
        )
    )
    assert direct.status is EnumContextPackBuilderStatus.OK
    assert direct.context_pack is not None
    # Same chunks as the runner's pack (pack_hash differs only by generated_at).
    assert [c.content for c in direct.context_pack.chunks] == ["chain text"]
    assert len(pack_hash) == 64


def test_over_budget_is_the_builders_budget_fail() -> None:
    # The resolver passes ~20000 tokens through; the builder's 16k budget
    # rejects it as BUDGET_FAIL (never silent truncation).
    artifacts = _resolve(
        _source(EnumContextFactor.CLAUDE_MD, "x" * 80000, sectioned=False)
    )
    text, pack_hash, warnings, failure_stage = _build_context_pack(
        factor_subset=("claude_md",),
        artifact_content_map={},
        contract_hash=_CONTRACT_HASH,
        resolved_artifacts=artifacts,
    )
    assert failure_stage == EnumFailureStage.BUDGET_FAIL
    assert text == ""
    assert pack_hash == ""
    assert any("token_budget_exceeded" in w or "budget" in w for w in warnings)


def test_off_arm_assembles_empty() -> None:
    text, pack_hash, warnings, failure_stage = _build_context_pack(
        factor_subset=(),
        artifact_content_map={},
        contract_hash=_CONTRACT_HASH,
        resolved_artifacts=_resolve_all_factors(),
    )
    assert text == ""
    assert pack_hash == ""
    assert failure_stage == EnumFailureStage.PACK_BUILD
    assert warnings == []


def test_request_refuses_a_factor_from_both_inputs() -> None:
    artifacts = _resolve(_source(EnumContextFactor.GOLDEN_CHAIN, "a", sectioned=False))
    with pytest.raises(ValidationError, match="golden_chain"):
        ModelContextRoiRunRequest(
            run_id="run-wiring",
            tasks=(ModelContextRoiTask(task_id="t", task_description="d"),),
            arms=(ModelContextRoiArmSpec(label="off"),),
            artifacts=artifacts,
            artifact_content_map={"golden_chain": "b"},
        )


def test_runner_handle_injects_resolver_artifacts() -> None:
    published: list[tuple[str, bytes]] = []
    event: dict[str, Any] = {
        "attempt_count": 1,
        "contract_passed": True,
        "first_pass_success": True,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "cost_inference_usd": 0.0,
        "model_id": "m",
        "provider": "local",
        "endpoint_class": "local-coder",
    }
    handler = HandlerContextRoiRunner(
        event_publisher=lambda topic, payload: published.append((topic, payload)),
        event_consumer=lambda _topic, _correlation_id, _timeout: dict(event),
        runner_contract_path=_RUNNER_CONTRACT_PATH,
    )
    request = ModelContextRoiRunRequest(
        run_id="run-wiring",
        tasks=(ModelContextRoiTask(task_id="t", task_description="d"),),
        arms=(
            ModelContextRoiArmSpec(label="claude_only", factor_subset=("claude_md",)),
        ),
        trials_per_cell=1,
        generation_timeout_seconds=5.0,
        contract_hash="wiring-hash",
        artifacts=_resolve(
            _source(
                EnumContextFactor.CLAUDE_MD,
                _FACTOR_BODIES[EnumContextFactor.CLAUDE_MD][0],
                sectioned=True,
            )
        ),
    )
    result = handler.handle(request)
    commands = [json.loads(p) for t, p in published if "node-generation-requested" in t]
    assert len(commands) == 1
    assert commands[0]["context_pack"].count("[claude_md]") == 2
    assert "No env defaults." in commands[0]["context_pack"]
    assert result.rows[0].context_pack_hash == commands[0]["context_pack_hash"] != ""
