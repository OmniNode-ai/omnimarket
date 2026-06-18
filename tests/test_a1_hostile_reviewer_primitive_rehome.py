# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A1 re-home contract tests (OMN-13208 / epic OMN-13205).

Phase A1 re-homes the four ``node_hostile_reviewer`` platform primitives into
canonical shared homes so no canonical node imports node-internal packages:

* inference bridge -> ``omnimarket.inference`` (OWNER, not shim)
* review-finding models -> ``omnimarket.models.model_review_finding``
* prompt builder -> ``omnimarket.review.prompt_builder``
* response parser -> ``omnimarket.review.response_parser``

The A1 exit gate: zero canonical Python import OR contract module declaration
of ``node_hostile_reviewer.*`` from outside the node's own tree.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).parent.parent / "src"

pytestmark = pytest.mark.unit


def test_inference_bridge_owner_is_omnimarket_inference() -> None:
    """The inference bridge is importable from the canonical owner package."""
    from omnimarket.inference.adapter_inference_bridge import (
        AdapterInferenceBridge,
        ModelInferenceAdapter,
        ModelInferenceBridgeConfig,
    )

    assert AdapterInferenceBridge is not None
    assert ModelInferenceAdapter is not None
    assert ModelInferenceBridgeConfig is not None


def test_review_finding_models_owner_is_omnimarket_models() -> None:
    """Review-finding models are importable from omnimarket.models."""
    from omnimarket.models.model_review_finding import (
        EnumFindingCategory,
        EnumFindingSeverity,
        EnumReviewConfidence,
        EnumReviewVerdict,
        ModelFindingEvidence,
        ModelReviewFinding,
    )

    assert EnumFindingCategory.SECURITY == "security"
    assert EnumFindingSeverity.CRITICAL == "critical"
    assert EnumReviewConfidence.HIGH == "high"
    assert EnumReviewVerdict.CLEAN == "clean"
    assert ModelFindingEvidence is not None
    assert ModelReviewFinding is not None


def test_prompt_builder_owner_is_omnimarket_review() -> None:
    """build_prompt + prompt models are importable from omnimarket.review."""
    from omnimarket.review.prompt_builder import (
        ModelPromptBuilderInput,
        ModelPromptBuilderOutput,
        build_prompt,
    )

    result = build_prompt(
        ModelPromptBuilderInput(
            prompt_template_id="adversarial_reviewer_pr",
            context_content="diff body",
            model_context_window=32_000,
        )
    )
    assert isinstance(result, ModelPromptBuilderOutput)
    assert "adversarial" in result.system_prompt.lower()


def test_response_parser_owner_is_omnimarket_review() -> None:
    """parse_model_response is importable from omnimarket.review."""
    from omnimarket.review.response_parser import (
        EnumParseStatus,
        ModelParseResult,
        parse_model_response,
    )

    result = parse_model_response(
        '[{"category": "security", "severity": "critical", '
        '"title": "t", "description": "d"}]',
        source_model="m",
    )
    assert isinstance(result, ModelParseResult)
    assert result.status == EnumParseStatus.SUCCESS
    assert len(result.findings) == 1


def test_no_external_import_of_node_hostile_reviewer_primitives() -> None:
    """A1 exit gate: no module outside node_hostile_reviewer imports its four
    re-homed primitive modules (bridge / review-finding model / prompt builder /
    response parser) via the node path.
    """
    rehomed_module_paths = (
        "omnimarket.nodes.node_hostile_reviewer.handlers.adapter_inference_bridge",
        "omnimarket.nodes.node_hostile_reviewer.handlers.handler_prompt_builder",
        "omnimarket.nodes.node_hostile_reviewer.handlers.handler_response_parser",
        "omnimarket.nodes.node_hostile_reviewer.models.model_review_finding",
    )
    offenders: list[str] = []
    import_patterns = tuple(
        re.compile(rf"^\s*(?:from|import)\s+{re.escape(mp)}\b")
        for mp in rehomed_module_paths
    )
    for py_file in _SRC_ROOT.rglob("*.py"):
        if "node_hostile_reviewer" in py_file.parts:
            continue
        for line in py_file.read_text(encoding="utf-8").splitlines():
            for module_path, pattern in zip(
                rehomed_module_paths, import_patterns, strict=True
            ):
                if pattern.match(line):
                    offenders.append(
                        f"{py_file.relative_to(_SRC_ROOT)} -> {module_path}"
                    )
    assert not offenders, (
        "External imports of re-homed node primitives remain:\n" + "\n".join(offenders)
    )


def test_no_external_contract_declaration_of_node_hostile_reviewer_bridge() -> None:
    """A1 exit gate: no contract.yaml outside the node declares the node's
    bridge handler module path.
    """
    needle = "node_hostile_reviewer.handlers.adapter_inference_bridge"
    offenders: list[str] = []
    for yaml_file in _SRC_ROOT.rglob("contract.yaml"):
        if "node_hostile_reviewer" in yaml_file.parts:
            continue
        text = yaml_file.read_text(encoding="utf-8")
        if needle in text:
            offenders.append(str(yaml_file.relative_to(_SRC_ROOT)))
    assert not offenders, (
        "External contract.yaml module declarations of the node bridge remain:\n"
        + "\n".join(offenders)
    )
