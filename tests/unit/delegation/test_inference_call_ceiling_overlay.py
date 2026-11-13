# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24039: the inference effect's per-call ceiling is read from the code budget overlay.

The contract no longer holds a ceiling; the overlay's ``class_budgets_s`` and
``ceiling_s`` are the source of truth, read on every call through
``require_call_budget_s`` / ``require_call_ceiling_s``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from omnibase_core.models.delegation.wire import ModelInferenceIntent

from omnimarket.handlers.routing.measured_correct_ceiling import (
    CODE_BUDGET_OVERLAY_ID,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    HandlerInferenceIntent,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_inference_call_budget import (
    INFERENCE_TIMEOUT_LOG_TOKEN,
)

pytestmark = pytest.mark.unit


class _ClassedIntent(ModelInferenceIntent):
    """The installed core model may not carry task_type yet."""

    task_type: str | None = None


def _overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ceiling_s: float,
    class_budgets: dict[str, float],
) -> None:
    """Write the code budget overlay under ``tmp_path`` and point the env root there."""
    path = tmp_path / CODE_BUDGET_OVERLAY_ID / "overlay.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    budgets = "\n".join(f"  {k}: {v}" for k, v in class_budgets.items())
    path.write_text(
        (
            "class_budgets_s:\n"
            f"{budgets}\n"
            "sizing_margin: 1.5\n"
            "round_up_to_s: 60\n"
            "min_completed_samples: 3\n"
            f"ceiling_s: {ceiling_s}\n"
            "input_ceilings:\n"
            "  local-coder:\n"
            "    code_generation: 65536\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))


def _intent(
    *,
    task_type: str | None = None,
    timeout_seconds: float = 300.0,
    **overrides: object,
) -> _ClassedIntent:
    defaults: dict[str, object] = {
        "base_url": "http://localhost:8000/v1/chat/completions",
        "model": "Qwen3.8-27B",
        "system_prompt": "You are a helpful assistant.",
        "prompt": "Write a test.",
        "max_tokens": 512,
        "temperature": 0.3,
        "timeout_seconds": timeout_seconds,
        "correlation_id": uuid4(),
        "task_type": task_type,
    }
    defaults.update(overrides)
    return _ClassedIntent(**defaults)  # type: ignore[arg-type]


def _mock_client(mock_client_cls: MagicMock, *, post_side_effect: Any = None) -> Any:
    client = MagicMock()
    mock_client_cls.return_value.__enter__.return_value = client
    if post_side_effect is not None:
        client.post.side_effect = post_side_effect
        return client
    response = MagicMock()
    response.json.return_value = {
        "id": "chatcmpl-abc",
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    response.raise_for_status.return_value = None
    client.post.return_value = response
    return client


def test_the_posted_timeout_is_the_overlay_class_budget_not_the_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Golden: {reasoning: 300} against a 300 s request posts with 300.0.

    The failing control: at the merge base the request was clamped to the
    contract's 120 s, so an overlay budget of 300 could not reach the wire.
    """
    _overlay(
        tmp_path,
        monkeypatch,
        ceiling_s=600,
        class_budgets={"reasoning": 300},
    )
    intent = _intent(task_type="reasoning", timeout_seconds=300.0)

    with patch("httpx.Client") as mock_client_cls:  # onex-allow-faked-boundary
        client = _mock_client(mock_client_cls)
        HandlerInferenceIntent().handle(intent)

    assert mock_client_cls.call_args.kwargs["timeout"] == 300.0
    assert client.post.call_args.kwargs["timeout"] == 300.0


def test_class_budget_below_request_clamps_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A class budget below the request clamps it: {reasoning: 90}, request 300 -> 90.0."""
    _overlay(
        tmp_path,
        monkeypatch,
        ceiling_s=600,
        class_budgets={"reasoning": 90},
    )
    intent = _intent(task_type="reasoning", timeout_seconds=300.0)

    with patch("httpx.Client") as mock_client_cls:  # onex-allow-faked-boundary
        client = _mock_client(mock_client_cls)
        HandlerInferenceIntent().handle(intent)

    assert mock_client_cls.call_args.kwargs["timeout"] == 90.0
    assert client.post.call_args.kwargs["timeout"] == 90.0


def test_overlay_edit_changes_the_ceiling_with_no_code_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An overlay edit changes the ceiling with no code change."""
    _overlay(
        tmp_path,
        monkeypatch,
        ceiling_s=600,
        class_budgets={"reasoning": 150},
    )
    intent = _intent(task_type="reasoning", timeout_seconds=300.0)

    with patch("httpx.Client") as mock_client_cls:  # onex-allow-faked-boundary
        client = _mock_client(mock_client_cls)
        HandlerInferenceIntent().handle(intent)

    assert mock_client_cls.call_args.kwargs["timeout"] == 150.0
    assert client.post.call_args.kwargs["timeout"] == 150.0

    # Rewrite the overlay with a higher budget; the next call reads the new value.
    _overlay(
        tmp_path,
        monkeypatch,
        ceiling_s=600,
        class_budgets={"reasoning": 200},
    )
    with patch("httpx.Client") as mock_client_cls:  # onex-allow-faked-boundary
        client = _mock_client(mock_client_cls)
        HandlerInferenceIntent().handle(intent)

    assert mock_client_cls.call_args.kwargs["timeout"] == 200.0
    assert client.post.call_args.kwargs["timeout"] == 200.0


def test_intent_with_no_task_type_gets_the_overlay_ceiling_s(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An intent with no task_type gets the overlay ceiling_s: 240, request 300 -> 240.0."""
    _overlay(
        tmp_path,
        monkeypatch,
        ceiling_s=240,
        # The overlay must still be a valid one (class_budgets_s is non-empty);
        # an intent that names no class reads ceiling_s, never a class budget.
        class_budgets={"code_generation": 120},
    )
    intent = _intent(task_type=None, timeout_seconds=300.0)

    with patch("httpx.Client") as mock_client_cls:  # onex-allow-faked-boundary
        client = _mock_client(mock_client_cls)
        HandlerInferenceIntent().handle(intent)

    assert mock_client_cls.call_args.kwargs["timeout"] == 240.0
    assert client.post.call_args.kwargs["timeout"] == 240.0


def test_unnamed_task_class_refuses_no_outbound_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task_type the overlay does not name refuses; no outbound post."""
    _overlay(
        tmp_path,
        monkeypatch,
        ceiling_s=600,
        class_budgets={"reasoning": 300},
    )
    intent = _intent(task_type="document", timeout_seconds=300.0)

    with patch("httpx.Client") as mock_client_cls:  # onex-allow-faked-boundary
        client = _mock_client(mock_client_cls)
        result = HandlerInferenceIntent().handle(intent)

    assert client.post.call_count == 0
    assert "document" in result.error_message


def test_no_overlay_root_refuses_no_outbound_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No overlay root names the code budget overlay; the same refusal, no outbound post."""
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    intent = _intent(task_type="reasoning", timeout_seconds=300.0)

    with patch("httpx.Client") as mock_client_cls:  # onex-allow-faked-boundary
        client = _mock_client(mock_client_cls)
        result = HandlerInferenceIntent().handle(intent)

    assert client.post.call_count == 0
    assert "reasoning" in result.error_message


def test_a_timed_out_rung_emits_one_greppable_line_naming_the_correlation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A timed-out call's error message contains 'overlay ceiling' and the log token appears once."""
    _overlay(
        tmp_path,
        monkeypatch,
        ceiling_s=120,
        class_budgets={"reasoning": 120},
    )
    intent = _intent(task_type="reasoning", timeout_seconds=300.0)

    with (
        caplog.at_level(logging.WARNING),
        patch("httpx.Client") as mock_client_cls,  # onex-allow-faked-boundary
    ):
        _mock_client(
            mock_client_cls,
            post_side_effect=httpx.TimeoutException(""),
        )
        result = HandlerInferenceIntent().handle(intent)

    assert "overlay ceiling" in result.error_message

    timeout_lines = [
        record.getMessage()
        for record in caplog.records
        if INFERENCE_TIMEOUT_LOG_TOKEN in record.getMessage()
    ]
    assert len(timeout_lines) == 1, caplog.text
    line = timeout_lines[0]
    assert str(intent.correlation_id) in line
    assert "Qwen3.8-27B" in line
    assert "elapsed_seconds=" in line
    assert "resolved_timeout_seconds=" in line
    assert "requested_timeout_seconds=300.0" in line
