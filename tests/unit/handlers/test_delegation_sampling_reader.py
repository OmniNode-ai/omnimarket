# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The sampling reader: overlay values, absences and refusals."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers.delegation_call_budget_reader import read_call_budget_s
from omnimarket.handlers.delegation_sampling_reader import read_sampling_settings
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.handlers.routing.measured_correct_ceiling import CODE_BUDGET_OVERLAY_ID
from omnimarket.models.model_delegation_code_budget import (
    EnumCodeBudgetRefusal,
    ModelCodeBudgetOverlay,
    ModelCodeBudgetRequest,
)
from omnimarket.nodes.node_delegation_code_budget_compute.handlers.handler_delegation_code_budget import (
    HandlerDelegationCodeBudget,
)

pytestmark = pytest.mark.unit


def _env(root: Path) -> dict[str, str]:
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


def _write_overlay(
    root: Path,
    code_generation: int = 600,
    class_sampling: bool = True,
    sampling_temperature: float = 0.7,
) -> Path:
    """Write the full code budget overlay under ``root`` and return its path."""
    path = root / CODE_BUDGET_OVERLAY_ID / "overlay.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    sampling = (
        "class_sampling:\n"
        "  code_generation:\n"
        f"    temperature: {sampling_temperature}\n"
        "    enable_thinking: false\n"
        "    max_tokens: 8192\n"
        if class_sampling
        else ""
    )
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
            "    code_generation: 65536\n" + sampling
        ),
        encoding="utf-8",
    )
    return path


def test_golden_named_class_returns_its_values(tmp_path: Path) -> None:
    """A named class returns its sampling values (code_generation)."""
    _write_overlay(tmp_path)
    sampling = read_sampling_settings("code_generation", _env(tmp_path))
    assert sampling is not None
    assert sampling.temperature == 0.7
    assert sampling.enable_thinking is False
    assert sampling.max_tokens == 8192


def test_unnamed_class_returns_none(tmp_path: Path) -> None:
    """A class the overlay does not name has no sampling settings."""
    _write_overlay(tmp_path)
    assert read_sampling_settings("document", _env(tmp_path)) is None


def test_overlay_without_class_sampling_returns_none(tmp_path: Path) -> None:
    """An overlay without class_sampling returns None for every class."""
    _write_overlay(tmp_path, class_sampling=False)
    assert read_sampling_settings("code_generation", _env(tmp_path)) is None
    assert read_call_budget_s("code_generation", _env(tmp_path)) == 600.0


def test_no_overlay_returns_none(tmp_path: Path) -> None:
    """An install with no overlay root naming the overlay has no settings."""
    assert read_sampling_settings("code_generation", _env(tmp_path)) is None


def test_invalid_overlay_refuses_never_defaults(tmp_path: Path) -> None:
    """A present but invalid overlay raises; the reader never returns a default."""
    path = tmp_path / CODE_BUDGET_OVERLAY_ID / "overlay.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        (
            "class_budgets_s:\n"
            "  code_generation: 600\n"
            "sizing_margin: 1.5\n"
            "round_up_to_s: 60\n"
            "min_completed_samples: 3\n"
            "ceiling_s: 600\n"
            "input_ceilings:\n"
            "  local-coder:\n"
            "    code_generation: 65536\n"
            "class_sampling:\n"
            "  code_generation: {}\n"
        ),
        encoding="utf-8",
    )
    with pytest.raises(NodeOverlayError):
        read_sampling_settings("code_generation", _env(tmp_path))


def test_overlay_edit_changes_the_value(tmp_path: Path) -> None:
    """The overlay is read on every call, so an edit is in force on the next read."""
    _write_overlay(tmp_path)
    assert read_sampling_settings("code_generation", _env(tmp_path)).temperature == 0.7
    _write_overlay(tmp_path, sampling_temperature=0.9)
    assert read_sampling_settings("code_generation", _env(tmp_path)).temperature == 0.9


def test_reader_is_the_node_sampling(tmp_path: Path) -> None:
    """The reader returns exactly the node's sampling value for each named class."""
    _write_overlay(tmp_path)
    overlay = load_structured_overlay(
        CODE_BUDGET_OVERLAY_ID, ModelCodeBudgetOverlay, _env(tmp_path)
    )
    for task_class in overlay.class_sampling:
        expected = (
            HandlerDelegationCodeBudget()
            .handle(
                ModelCodeBudgetRequest(
                    operation="sampling", task_class=task_class, overlay=overlay
                )
            )
            .sampling
        )
        assert read_sampling_settings(task_class, _env(tmp_path)) == expected


def test_handler_refuses_unnamed_class(tmp_path: Path) -> None:
    """The handler refuses a class the overlay does not name with UNNAMED_SAMPLING_CLASS."""
    _write_overlay(tmp_path)
    overlay = load_structured_overlay(
        CODE_BUDGET_OVERLAY_ID, ModelCodeBudgetOverlay, _env(tmp_path)
    )
    result = HandlerDelegationCodeBudget().handle(
        ModelCodeBudgetRequest(
            operation="sampling", task_class="document", overlay=overlay
        )
    )
    assert result.refusal is EnumCodeBudgetRefusal.UNNAMED_SAMPLING_CLASS
    assert result.sampling is None
