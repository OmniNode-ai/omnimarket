# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pins for the fan-out's READY control and its test-shaped admission rule.

The shape-directive table lives in the same repository as the node, so the pin
reads it from the contract file at test time and never skips: an absent or
emptied table fails.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_delegate_fanout_effect.handlers.handler_delegate_fanout import (
    READY_CONTROL_PROMPT,
    admit,
)
from omnimarket.nodes.node_delegate_fanout_effect.models.model_fanout_request import (
    ModelFanoutItem,
)

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/configs/task_class_contracts.v1.yaml"
)
# The prompt that resolved `unconstrained` on the dev lane on 2026-09-21 and was
# refused four times for being a bare single-word fragment.
UNDECLARED_PROMPT = (
    "Name the capital city of France. Answer with the city name and nothing else."
)


def _directive_patterns() -> list[str]:
    data = yaml.safe_load(CONTRACT.read_text())
    table = data["response_shape_directives"]
    return [pattern for patterns in table.values() for pattern in patterns]


def _matches(prompt: str, patterns: list[str]) -> list[str]:
    return [p for p in patterns if re.search(p, prompt, re.IGNORECASE)]


def test_the_shape_directive_contract_is_reachable() -> None:
    assert CONTRACT.is_file(), f"contract file not found at {CONTRACT}"
    table = yaml.safe_load(CONTRACT.read_text())["response_shape_directives"]
    assert table["exact_literal"], "exact_literal directive list is empty"


def test_the_ready_control_prompt_matches_a_declared_shape_directive() -> None:
    assert _matches(READY_CONTROL_PROMPT, _directive_patterns())


def test_a_reworded_control_prompt_matches_no_declared_shape_directive() -> None:
    reworded = "Reply with the word READY and nothing else."
    assert not _matches(reworded, _directive_patterns())


def test_the_shape_directive_matcher_discriminates() -> None:
    patterns = _directive_patterns()
    assert _matches(READY_CONTROL_PROMPT, patterns), "positive control"
    assert not _matches(UNDECLARED_PROMPT, patterns)


def test_a_test_shaped_item_without_a_task_type_is_refused() -> None:
    for task_type in (None, "", "   "):
        verdict = admit(
            ModelFanoutItem(
                label="a", prompt="List tests", test_shaped=True, task_type=task_type
            )
        )
        assert verdict["ok"] is False
        assert "test_shaped" in str(verdict["reason"])


def test_a_test_shaped_item_with_a_task_type_is_admitted() -> None:
    verdict = admit(
        ModelFanoutItem(
            label="a", prompt="List tests", test_shaped=True, task_type="planning"
        )
    )
    assert verdict == {"ok": True}


@pytest.mark.parametrize("task_type", [None, "planning"])
def test_an_item_not_marked_test_shaped_is_not_refused_for_the_flag(
    task_type: str | None,
) -> None:
    verdict = admit(
        ModelFanoutItem(label="a", prompt="List tests", task_type=task_type)
    )
    assert "test_shaped" not in str(verdict.get("reason", ""))
