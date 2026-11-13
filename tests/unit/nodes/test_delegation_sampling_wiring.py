# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local dispatch port applies the overlay's sampling values of a task class (OMN-24061)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.routing.measured_correct_ceiling import CODE_BUDGET_OVERLAY_ID
from omnimarket.models.model_delegation_code_budget import ModelClassSampling
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port,
)

pytestmark = pytest.mark.unit

_BASE_OVERLAY = (
    "class_budgets_s:\n"
    "  code_generation: 600\n"
    "  document: 180\n"
    "sizing_margin: 1.5\n"
    "round_up_to_s: 60\n"
    "min_completed_samples: 3\n"
    "ceiling_s: 600\n"
    "input_ceilings:\n"
    "  local-coder:\n"
    "    code_generation: 65536\n"
)


def _overlay(root: Path, class_sampling: str) -> dict[str, str]:
    path = root / CODE_BUDGET_OVERLAY_ID / "overlay.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_BASE_OVERLAY + class_sampling, encoding="utf-8")
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


@pytest.fixture
def overlay_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    return tmp_path


def test_golden_overlay_values_go_over_the_profile_options() -> None:
    sampling = ModelClassSampling(
        temperature=0.7,
        enable_thinking=True,
        thinking_token_budget=2048,
        top_p=0.8,
    )
    options, prompt = port._apply_class_sampling(
        {"chat_template_kwargs": {"enable_thinking": False}},
        "/no_think\nhello",
        sampling,
    )
    assert options["chat_template_kwargs"] == {"enable_thinking": True}
    assert options["thinking_token_budget"] == 2048
    assert options["thinking_budget_tokens"] == 2048
    assert options["top_p"] == 0.8
    assert "top_k" not in options
    assert prompt == "hello"


def test_existing_chat_template_key_is_kept_beside_enable_thinking() -> None:
    sampling = ModelClassSampling(enable_thinking=False)
    options, prompt = port._apply_class_sampling(
        {"chat_template_kwargs": {"other": 1}}, "/no_think\nhello", sampling
    )
    assert options["chat_template_kwargs"] == {"other": 1, "enable_thinking": False}
    assert prompt == "/no_think\nhello"


def test_a_value_the_overlay_does_not_name_changes_nothing() -> None:
    sampling = ModelClassSampling(max_tokens=512)
    given = {"top_k": 20}
    options, prompt = port._apply_class_sampling(given, "/no_think\nhello", sampling)
    assert options == {"top_k": 20}
    assert prompt == "/no_think\nhello"


def test_the_input_options_are_not_mutated() -> None:
    given: dict[str, object] = {"chat_template_kwargs": {"enable_thinking": False}}
    port._apply_class_sampling(
        given, "p", ModelClassSampling(enable_thinking=True, top_p=0.5)
    )
    assert given == {"chat_template_kwargs": {"enable_thinking": False}}


def test_named_class_fills_what_the_caller_left_out(overlay_env: Path) -> None:
    _overlay(
        overlay_env,
        "class_sampling:\n  code_generation:\n    temperature: 0.7\n    max_tokens: 8192\n",
    )
    assert port._resolve_class_sampling_call_values("code_generation", None, None) == (
        8192,
        0.7,
    )


def test_a_caller_value_wins_over_the_overlay(overlay_env: Path) -> None:
    _overlay(
        overlay_env,
        "class_sampling:\n  code_generation:\n    temperature: 0.7\n    max_tokens: 8192\n",
    )
    assert port._resolve_class_sampling_call_values("code_generation", 256, 0.1) == (
        256,
        0.1,
    )


def test_unnamed_class_and_missing_table_leave_the_call_as_it_was(
    overlay_env: Path,
) -> None:
    _overlay(
        overlay_env,
        "class_sampling:\n  code_generation:\n    temperature: 0.7\n",
    )
    assert port._resolve_class_sampling_call_values("document", None, None) == (
        None,
        None,
    )
    _overlay(overlay_env, "")
    assert port._resolve_class_sampling_call_values("code_generation", None, None) == (
        None,
        None,
    )


def test_no_overlay_root_leaves_the_call_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    assert port._resolve_class_sampling_call_values("code_generation", 100, None) == (
        100,
        None,
    )


def test_invalid_overlay_propagates_and_never_defaults(overlay_env: Path) -> None:
    _overlay(overlay_env, "class_sampling:\n  code_generation: {}\n")
    with pytest.raises(NodeOverlayError):
        port._resolve_class_sampling_call_values("code_generation", None, None)
