# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""harness_backend_declared: claude-glm and codex are declared harness backends, not literals (OMN-26219)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumDelegationBackendKind,
    EnumDelegationHarness,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_harness_escalation_chain import (
    resolve_class_escalation_chain,
)
from tests.unit.delegation.harness_support_omn26219 import (
    call_request,
    load_overlay,
)

pytestmark = pytest.mark.unit

CODE_CLASSES = ("code_generation", "code_review", "test")
NODE_SRC = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_llm_delegation_call_effect"
)


def test_harness_backend_declared_claude_glm_rungs_for_code_classes() -> None:
    overlay = load_overlay()
    request = call_request()
    for task_class in CODE_CLASSES:
        chain = resolve_class_escalation_chain(
            task_class,
            tenant_id=request.tenant_slug,
            surface=request.surface,
            overlay=overlay,
        )
        assert chain is not None
        glm_models = [
            rung.model_name
            for rung in chain.rungs
            if rung.kind == "harness"
            and overlay.backend_by_id()[rung.backend_id or ""].harness
            is EnumDelegationHarness.CLAUDE_GLM
        ]
        assert glm_models[:2] == ["glm-5.3-flash", "glm-5.3"], task_class


def test_harness_backend_declared_claude_glm_is_a_routable_harness_backend() -> None:
    backends = load_overlay().backend_by_id()
    glm = [
        b for b in backends.values() if b.harness is EnumDelegationHarness.CLAUDE_GLM
    ]
    assert {b.model_name for b in glm} == {"glm-5.3-flash", "glm-5.3"}
    for backend in glm:
        assert backend.kind is EnumDelegationBackendKind.HARNESS
        assert backend.explicit_pin_only is False
        assert backend.endpoint_url is None
        assert set(CODE_CLASSES) <= set(backend.capabilities)


def test_harness_backend_declared_codex_is_pin_only_and_code_classes_only() -> None:
    codex = [
        b
        for b in load_overlay().backend_by_id().values()
        if b.harness is EnumDelegationHarness.CODEX
    ]
    assert len(codex) == 1
    assert codex[0].kind is EnumDelegationBackendKind.HARNESS
    assert codex[0].explicit_pin_only is True
    assert codex[0].model_name is None
    assert set(codex[0].capabilities) <= {
        "code_generation",
        "test",
        "code_review",
        "refactor",
    }


def test_harness_backend_declared_no_model_name_literal_in_the_node_source() -> None:
    """AC1 falsifier: no GLM model name or Codex account name in the node's own source, tests aside."""
    pattern = re.compile(r"glm-5|codex-account")
    found = [
        f"{path.relative_to(NODE_SRC)}:{number}"
        for path in sorted(NODE_SRC.rglob("*.py"))
        if "tests" not in path.relative_to(NODE_SRC).parts
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if pattern.search(line)
    ]
    assert found == []


def test_harness_backend_declared_control_the_scan_finds_a_planted_literal(
    tmp_path: Path,
) -> None:
    """Positive control: the pattern of the scan above matches a literal when one exists."""
    planted = tmp_path / "handler.py"
    planted.write_text('MODEL = "glm-5.3-flash"\n')
    assert re.search(r"glm-5|codex-account", planted.read_text()) is not None


@pytest.mark.parametrize(
    "topic",
    [
        "onex.evt.omnimarket.delegation-harness-completed.v1",
        "onex.evt.omnimarket.delegation-harness-attempt-recorded.v1",
    ],
)
def test_node_llm_delegation_call_effect_declares_harness_output_topic(
    topic: str,
) -> None:
    """The harness terminal and the per-attempt cost record are declared outputs of the node."""
    import yaml

    contract = yaml.safe_load((NODE_SRC / "contract.yaml").read_text())
    assert topic in contract["event_bus"]["publish_topics"]
    assert topic in contract["externally_consumed_topics"]
