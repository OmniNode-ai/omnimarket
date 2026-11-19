# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""/config says what is set up in one short line (OMN-23413).

Operator 2026-11-13, screenshot of /config with 34 models set up: "why this
text", a block of seven lines naming every model. Failure modes:

  B1  the line names every model however many there are, so it grows with the
      selection; or it hides the names (and their servers) when they fit.
  B2  the count is not the number of models set up, or a provider is missing.
  B3  nothing set up still reads as "nothing set up".
"""

from __future__ import annotations

import pytest

from omnimarket.handlers.model_catalog import ModelCatalogEntry
from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    ModelTuiSelection,
    SetupScreen,
    _say,
)
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import _Runner

pytestmark = pytest.mark.unit


def _entry(provider: str, model: str) -> ModelCatalogEntry:
    return ModelCatalogEntry(
        provider=provider, model_id=model, display_name=model, deployment="cloud"
    )


def _line(*entries: ModelCatalogEntry) -> str:
    screen = SetupScreen(_Runner([]), selection=ModelTuiSelection(entries=entries))
    return screen._current_line()


def test_many_models_read_as_one_count_per_provider() -> None:
    """B1, B2: the selection read on 2026-11-13, 18 + 15 + 1."""
    line = _line(
        *(_entry("gemini", f"gemini-{n}") for n in range(18)),
        *(_entry("openrouter", f"vendor/model-{n}:free") for n in range(15)),
        _entry("reflection", "Beam-1.0-501B-A23B"),
    )
    assert "34 models" in line
    assert "Google Gemini 18" in line
    assert "OpenRouter 15" in line
    assert "Reflection AI 1" in line
    assert "gemini-0" not in line
    assert "vendor/model" not in line
    assert len(line) < 140  # one line in the 160-column tab


def test_a_few_models_are_named() -> None:
    """B1: names that fit one line stay, so a server's model shows its server."""
    line = _line(_entry("reflection", "Beam-1.0-501B-A23B"))
    assert "Beam-1.0-501B-A23B" in line
    assert "1 model" not in line


def test_nothing_set_up_says_so() -> None:
    """B3."""
    assert _line() == _say("basket_empty")
