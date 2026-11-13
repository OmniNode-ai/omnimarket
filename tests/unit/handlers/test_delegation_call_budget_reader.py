# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The call budget reader: overlay values, absences and refusals (OMN-24023)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers.delegation_call_budget_reader import read_call_budget_s
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.handlers.routing.measured_correct_ceiling import CODE_BUDGET_OVERLAY_ID
from omnimarket.models.model_delegation_code_budget import (
    ModelCodeBudgetOverlay,
    ModelCodeBudgetRequest,
)
from omnimarket.nodes.node_delegation_code_budget_compute.handlers.handler_delegation_code_budget import (
    HandlerDelegationCodeBudget,
)

pytestmark = pytest.mark.unit


def _env(root: Path) -> dict[str, str]:
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


def _write_overlay(root: Path, code_generation: int = 600) -> Path:
    """Write the full code budget overlay under ``root`` and return its path."""
    path = root / CODE_BUDGET_OVERLAY_ID / "overlay.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        (
            "class_budgets_s:\n"
            f"  code_generation: {code_generation}\n"
            "  test: 480\n"
            "  code_review: 600\n"
            "sizing_margin: 1.5\n"
            "round_up_to_s: 60\n"
            "min_completed_samples: 3\n"
            "ceiling_s: 600\n"
            "input_ceilings:\n"
            "  local-coder:\n"
            "    code_generation: 65536\n"
        ),
        encoding="utf-8",
    )
    return path


def test_golden_named_class_returns_overlay_value(tmp_path: Path) -> None:
    """A named class reads its overlay budget (code_generation 600, test 480)."""
    _write_overlay(tmp_path)
    assert read_call_budget_s("code_generation", _env(tmp_path)) == 600.0
    assert read_call_budget_s("test", _env(tmp_path)) == 480.0


def test_unnamed_class_returns_none(tmp_path: Path) -> None:
    """A class the overlay does not name has no budget."""
    _write_overlay(tmp_path)
    assert read_call_budget_s("document", _env(tmp_path)) is None


def test_no_overlay_returns_none(tmp_path: Path) -> None:
    """An install with no overlay root naming the code budget overlay has no budget."""
    assert read_call_budget_s("code_generation", _env(tmp_path)) is None


def test_invalid_overlay_refuses_never_defaults(tmp_path: Path) -> None:
    """A present but invalid overlay raises; the reader never returns a default."""
    path = tmp_path / CODE_BUDGET_OVERLAY_ID / "overlay.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        (
            "class_budgets_s: {}\n"
            "sizing_margin: 1.5\n"
            "round_up_to_s: 60\n"
            "min_completed_samples: 3\n"
            "ceiling_s: 600\n"
            "input_ceilings:\n"
            "  local-coder:\n"
            "    code_generation: 65536\n"
        ),
        encoding="utf-8",
    )
    with pytest.raises(NodeOverlayError):
        read_call_budget_s("code_generation", _env(tmp_path))


def test_overlay_edit_changes_the_value(tmp_path: Path) -> None:
    """The overlay is read on every call, so an edit is in force on the next read."""
    _write_overlay(tmp_path)
    assert read_call_budget_s("code_generation", _env(tmp_path)) == 600.0
    _write_overlay(tmp_path, code_generation=700)
    assert read_call_budget_s("code_generation", _env(tmp_path)) == 700.0


def test_reader_is_the_node_resolve(tmp_path: Path) -> None:
    """The reader returns exactly the node's resolve value for each overlay class."""
    _write_overlay(tmp_path)
    overlay = load_structured_overlay(
        CODE_BUDGET_OVERLAY_ID, ModelCodeBudgetOverlay, _env(tmp_path)
    )
    for task_class in overlay.class_budgets_s:
        expected = (
            HandlerDelegationCodeBudget()
            .handle(
                ModelCodeBudgetRequest(
                    operation="resolve", task_class=task_class, overlay=overlay
                )
            )
            .call_budget_s
        )
        assert read_call_budget_s(task_class, _env(tmp_path)) == expected
