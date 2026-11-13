# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain of node_hook_tool_outcome_compute."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_hook_tool_outcome_compute.handlers import (
    HandlerHookToolOutcome,
)
from omnimarket.nodes.node_hook_tool_outcome_compute.models import (
    ModelHookToolOutcomeRequest,
    ModelRefusalRule,
    ModelToolOutcomeRules,
)
from omnimarket.nodes.node_hook_tool_outcome_compute.overlay import (
    load_outcome_rules,
)


def test_invalid_hook_event_name_is_a_validation_error() -> None:
    with pytest.raises(ValidationError):
        ModelHookToolOutcomeRequest(
            tool_name="Bash",
            hook_event_name="PreToolUse",  # type: ignore[arg-type]
        )


def test_rule_with_a_pattern_that_does_not_compile_is_refused() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ModelRefusalRule(
            name="bad",
            pattern="[",
            component="x",
            tool_names=("Bash",),
        )
    assert "bad" in str(excinfo.value)


def test_rule_with_neither_component_group_nor_fixed_component_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelRefusalRule(
            name="no_component",
            pattern="^REFUSED",
            tool_names=("Bash",),
        )


def test_overlay_root_with_no_overlay_file_raises_node_overlay_error() -> None:
    with pytest.raises(NodeOverlayError) as excinfo:
        load_outcome_rules(environ={"ONEX_SKILL_OVERLAY_ROOTS": "/nonexistent-root"})
    assert "node_hook_tool_outcome_compute" in str(excinfo.value)


def test_without_the_rules_a_refusal_reads_as_ok() -> None:
    rules = ModelToolOutcomeRules(
        refusal_rules=(),
        timeout_markers=(),
        interrupted_is_timeout=False,
        timeout_exit_codes=(),
        exit_code_pattern="^Exit code ([0-9]+)",
    )
    handler = HandlerHookToolOutcome(rules)
    result = handler.handle(
        ModelHookToolOutcomeRequest(
            tool_name="Bash",
            hook_event_name="PostToolUse",
            tool_response="REFUSED factory-floor-publish: the required step failed",
            exit_code=0,
        )
    )
    assert result.outcome.value == "ok"
