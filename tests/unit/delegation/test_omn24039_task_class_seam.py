# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24079 seam: the task class the producer stamps is the class the effect reads its ceiling for.

The orchestrator builds the inference intent (``_build_model_inference_intent``) and the inference effect
resolves the call ceiling from it (``_ceiling_seconds_for``). Both sides meet at the intent's ``task_type``:
this test fails when either side stops carrying it, and it needs the omnibase_core model that has the field.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import (
    EnumCredentialSource,
    ModelInferenceIntent,
)

from omnimarket.handlers.delegation_call_budget_reader import CallBudgetNotDeclaredError
from omnimarket.handlers.routing.measured_correct_ceiling import CODE_BUDGET_OVERLAY_ID
from omnimarket.nodes.node_delegation_orchestrator.handlers import (
    handler_delegation_workflow as producer,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_inference_intent as effect,
)

pytestmark = pytest.mark.unit


def _overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reasoning: int) -> None:
    path = tmp_path / CODE_BUDGET_OVERLAY_ID / "overlay.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        (
            "class_budgets_s:\n"
            f"  reasoning: {reasoning}\n"
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
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))


def _build(task_type: str | None) -> ModelInferenceIntent:
    return producer._build_model_inference_intent(
        base_url="http://localhost:8000/v1/chat/completions",
        model="m",
        system_prompt="s",
        prompt="p",
        max_tokens=64,
        temperature=0.3,
        timeout_seconds=300.0,
        correlation_id=uuid4(),
        inference_attempt_id=uuid4(),
        api_key_ref=None,
        expected_credential_source=EnumCredentialSource.NONE,
        route=None,
        provider=None,
        extra_headers=None,
        provider_request_options={},
        response_format=None,
        response_contract_sha256=None,
        response_contract_output_shape=None,
        response_contract_instruction=None,
        tenant_id="t",
        task_type=task_type,
    )


def test_the_core_model_carries_task_type() -> None:
    """The installed core's ModelInferenceIntent declares task_type (omnibase_core#2316)."""
    assert "task_type" in ModelInferenceIntent.model_fields


def test_the_stamped_class_is_the_class_the_effect_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Producer stamps reasoning; the effect's ceiling is the overlay's reasoning budget, and follows an overlay edit."""
    _overlay(tmp_path, monkeypatch, reasoning=300)
    intent = _build("reasoning")
    assert intent.task_type == "reasoning"
    assert effect._ceiling_seconds_for(intent) == 300.0
    _overlay(tmp_path, monkeypatch, reasoning=150)
    assert effect._ceiling_seconds_for(intent) == 150.0


def test_a_producer_with_no_class_stamps_none_and_the_effect_uses_the_overlay_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: no class on the producer side is no class on the wire, and the ceiling is the overlay's ceiling_s."""
    _overlay(tmp_path, monkeypatch, reasoning=300)
    intent = _build(None)
    assert intent.task_type is None
    assert effect._ceiling_seconds_for(intent) == 600.0


def test_a_class_the_overlay_does_not_name_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task class the overlay does not name is refused: the effect raises the budget refusal, no fallback."""
    path = tmp_path / CODE_BUDGET_OVERLAY_ID / "overlay.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        (
            "class_budgets_s:\n"
            "  code_generation: 300\n"
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
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    intent = _build("reasoning")

    with pytest.raises(CallBudgetNotDeclaredError):
        effect._ceiling_seconds_for(intent)
