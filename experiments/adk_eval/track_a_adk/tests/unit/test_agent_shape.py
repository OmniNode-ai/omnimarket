# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for the Track A ADK agent scaffold.

These tests verify construction-time invariants only (model id, tool
registration, AI-Studio auth path) — NOT end-to-end Gemini calls. The
end-to-end run + validation is covered by the harness + scorer tracked in
later plan tasks (P8/P9).
"""

from __future__ import annotations

import json
import os

import pytest

os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"

from app.agent import root_agent
from app.run_agent import _strip_fences


def test_agent_name() -> None:
    assert root_agent.name == "type_debt_scout_adk"


def test_agent_has_single_tool() -> None:
    tool_names = [
        t.__name__ if callable(t) else type(t).__name__ for t in root_agent.tools
    ]
    assert tool_names == ["run_mypy_and_parse"]


def test_agent_instruction_is_json_first() -> None:
    """Instruction must ask for a JSON envelope compatible with ModelTypeDebtReport."""
    instr = root_agent.instruction or ""
    assert "ModelTypeDebtPriority" in instr
    assert '"tool": "adk"' in instr
    assert '"findings_prioritized"' in instr


def test_ai_studio_auth_path_selected() -> None:
    # Scaffold default was Vertex; we override to AI Studio before ADK import.
    assert os.environ.get("GOOGLE_GENAI_USE_VERTEXAI") == "False"


@pytest.mark.parametrize(
    ("raw", "expected_first_char"),
    [
        ('```json\n{"tool": "adk"}\n```', "{"),
        ('```\n{"tool": "adk"}\n```', "{"),
        ('{"tool": "adk"}', "{"),
    ],
)
def test_strip_fences_removes_markdown(raw: str, expected_first_char: str) -> None:
    stripped = _strip_fences(raw)
    assert stripped.startswith(expected_first_char)
    # Round-trips cleanly as JSON
    assert json.loads(stripped) == {"tool": "adk"}
