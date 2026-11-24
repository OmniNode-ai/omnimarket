# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A backend carries its own reasoning effort in the code budget overlay (OMN-26365).

``class_sampling`` is shared by every backend of a task class, so it cannot hold a setting only one
model understands. ``backend_sampling`` is keyed by backend id and rides the request of that backend
alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.handlers.delegation_sampling_reader import read_backend_sampling
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.routing.measured_correct_ceiling import CODE_BUDGET_OVERLAY_ID
from omnimarket.models.model_delegation_code_budget import (
    EnumCodeBudgetRefusal,
    ModelBackendSampling,
    ModelClassSampling,
    ModelCodeBudgetOverlay,
    ModelCodeBudgetRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port,
)
from omnimarket.nodes.node_delegation_code_budget_compute.handlers.handler_delegation_code_budget import (
    HandlerDelegationCodeBudget,
)

pytestmark = pytest.mark.unit

_BASE_OVERLAY = (
    "class_budgets_s:\n"
    "  document: 180\n"
    "sizing_margin: 1.5\n"
    "round_up_to_s: 60\n"
    "min_completed_samples: 3\n"
    "ceiling_s: 600\n"
    "input_ceilings:\n"
    "  local-coder:\n"
    "    document: 65536\n"
)
_INKLING = "local-h201-inkling-chat"


def _overlay(root: Path, tail: str) -> dict[str, str]:
    path = root / CODE_BUDGET_OVERLAY_ID / "overlay.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_BASE_OVERLAY + tail, encoding="utf-8")
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


def test_class_sampling_still_forbids_reasoning_effort() -> None:
    """The class table is shared by every backend, so it must not carry a one-model setting."""
    with pytest.raises(ValidationError):
        ModelClassSampling.model_validate({"reasoning_effort": 0.2})


@pytest.mark.parametrize("effort", [0.0, 0.2, 0.9, 0.99, "low", "high"])
def test_backend_sampling_accepts_an_effort_the_template_takes(
    effort: float | str,
) -> None:
    assert ModelBackendSampling(reasoning_effort=effort).reasoning_effort == effort


@pytest.mark.parametrize("effort", [-0.1, 1.0, "", "fast"])
def test_backend_sampling_refuses_an_effort_the_template_does_not_take(
    effort: float | str,
) -> None:
    with pytest.raises(ValidationError):
        ModelBackendSampling(reasoning_effort=effort)


def test_backend_sampling_forbids_extra_keys_and_empty_entries() -> None:
    with pytest.raises(ValidationError):
        ModelBackendSampling.model_validate({"reasoning_effort": 0.2, "top_p": 0.5})
    with pytest.raises(ValidationError):
        ModelBackendSampling.model_validate({})


def test_overlay_without_backend_sampling_is_valid_and_names_none(
    tmp_path: Path,
) -> None:
    env = _overlay(tmp_path, "")
    assert read_backend_sampling(_INKLING, env) is None


def test_overlay_names_a_backend_effort(tmp_path: Path) -> None:
    env = _overlay(
        tmp_path, f"backend_sampling:\n  {_INKLING}:\n    reasoning_effort: 0.2\n"
    )
    sampling = read_backend_sampling(_INKLING, env)
    assert sampling is not None
    assert sampling.reasoning_effort == 0.2


def test_a_backend_the_overlay_does_not_name_has_none(tmp_path: Path) -> None:
    env = _overlay(
        tmp_path, f"backend_sampling:\n  {_INKLING}:\n    reasoning_effort: 0.2\n"
    )
    assert read_backend_sampling("local-coder", env) is None


def test_no_overlay_root_names_none(tmp_path: Path) -> None:
    assert (
        read_backend_sampling(_INKLING, {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
        is None
    )


def test_empty_or_invalid_backend_sampling_refuses_never_defaults(
    tmp_path: Path,
) -> None:
    env = _overlay(tmp_path, "backend_sampling: {}\n")
    with pytest.raises(NodeOverlayError):
        read_backend_sampling(_INKLING, env)
    env = _overlay(tmp_path, f"backend_sampling:\n  {_INKLING}:\n    top_p: 0.5\n")
    with pytest.raises(NodeOverlayError):
        read_backend_sampling(_INKLING, env)


def test_handler_refuses_an_unnamed_backend_with_a_typed_refusal() -> None:
    overlay = ModelCodeBudgetOverlay.model_validate(
        {
            "class_budgets_s": {"document": 180},
            "sizing_margin": 1.5,
            "round_up_to_s": 60,
            "min_completed_samples": 3,
            "ceiling_s": 600,
            "input_ceilings": {"local-coder": {"document": 1}},
            "backend_sampling": {_INKLING: {"reasoning_effort": 0.2}},
        }
    )
    named = HandlerDelegationCodeBudget().handle(
        ModelCodeBudgetRequest(
            operation="backend_sampling",
            task_class="document",
            backend=_INKLING,
            overlay=overlay,
        )
    )
    assert named.refusal is None
    assert named.backend_sampling == ModelBackendSampling(reasoning_effort=0.2)
    unnamed = HandlerDelegationCodeBudget().handle(
        ModelCodeBudgetRequest(
            operation="backend_sampling",
            task_class="document",
            backend="local-coder",
            overlay=overlay,
        )
    )
    assert unnamed.refusal is EnumCodeBudgetRefusal.UNNAMED_BACKEND_SAMPLING
    assert unnamed.backend_sampling is None


def test_backend_sampling_request_names_a_backend() -> None:
    overlay = ModelCodeBudgetOverlay.model_validate(
        {
            "class_budgets_s": {"document": 180},
            "sizing_margin": 1.5,
            "round_up_to_s": 60,
            "min_completed_samples": 3,
            "ceiling_s": 600,
            "input_ceilings": {"local-coder": {"document": 1}},
        }
    )
    with pytest.raises(ValidationError):
        ModelCodeBudgetRequest(
            operation="backend_sampling", task_class="document", overlay=overlay
        )


def test_effort_rides_the_chat_template_of_that_backend_only() -> None:
    options = port._apply_backend_sampling(
        {"chat_template_kwargs": {"enable_thinking": True}, "top_p": 0.5},
        ModelBackendSampling(reasoning_effort=0.2),
    )
    assert options == {
        "chat_template_kwargs": {"enable_thinking": True, "reasoning_effort": 0.2},
        "top_p": 0.5,
    }


def test_a_backend_with_no_effort_changes_nothing() -> None:
    given = {"top_k": 20}
    assert port._apply_backend_sampling(given, None) == {"top_k": 20}


def test_the_input_options_are_not_mutated() -> None:
    given: dict[str, object] = {"chat_template_kwargs": {"enable_thinking": True}}
    port._apply_backend_sampling(given, ModelBackendSampling(reasoning_effort="low"))
    assert given == {"chat_template_kwargs": {"enable_thinking": True}}
