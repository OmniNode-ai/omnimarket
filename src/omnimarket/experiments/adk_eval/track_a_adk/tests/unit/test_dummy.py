# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Sanity tests for the local A2A parser path exposed by the Track A scaffold.

OMN-9639 makes the local ADK scout runnable over A2A. The exposed tool the
agent calls is ``app.agent.run_mypy_and_parse``, which delegates (via a
deferred import) to the shared parser at
``omnimarket.experiments.adk_eval.tools.mypy_parser``.

These tests cover the subproject-local invariants only — tool wiring,
agent-card metadata — so a regression that breaks A2A exposure or removes the
parser tool fails CI inside the track_a_adk venv (which does not carry the
parent ``omnimarket`` package). Parser-internal coverage lives at
``tests/unit/experiments/adk_eval/`` in the parent repo.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# AI Studio path, NOT Vertex. Match test_agent_shape.py — must be set before
# any google.adk import is resolved transitively.
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"

from app.agent import root_agent, run_mypy_and_parse


def test_run_mypy_and_parse_is_registered_as_agent_tool() -> None:
    """The A2A-exposed tool must be the parser wrapper, not a placeholder."""
    assert run_mypy_and_parse in root_agent.tools
    assert run_mypy_and_parse.__name__ == "run_mypy_and_parse"


def test_run_mypy_and_parse_signature_takes_repo_path() -> None:
    """A2A clients call this tool with a repo_path string; signature must match."""
    import inspect

    sig = inspect.signature(run_mypy_and_parse)
    params = list(sig.parameters.values())
    assert len(params) == 1
    assert params[0].name == "repo_path"
    assert params[0].annotation is str


def test_agent_card_advertises_local_a2a_url_on_loopback() -> None:
    """OMN-9639 makes the scout runnable over A2A — the card must point at a
    local loopback URL on the same port the FastAPI app binds (8000).
    """
    card_path = Path(__file__).resolve().parents[2] / "app" / "agent.json"
    card = json.loads(card_path.read_text(encoding="utf-8"))
    assert card["name"] == "type-debt-scout-adk"
    assert card["url"].startswith("http://127.0.0.1:8000/")
