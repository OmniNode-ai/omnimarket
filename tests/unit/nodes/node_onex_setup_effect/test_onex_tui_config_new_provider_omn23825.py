# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The TUI /config, after setup, asks a newly chosen provider for its key (OMN-23825 AC2).

Failure modes:
  F1. With a provider already set up, choosing another hosted provider on /config
      offers no key field, so its key cannot be given without another step first.
  F2. The key field shows the typed key, or the provider's models are fetched
      before a key is given.
  F3. The test runs on an empty setup, so it says nothing about "after setup".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_onex_setup_effect.cli_tui import OnexTui
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _hosted_listing,
    _open_config,
    _root,
    _Runner,
    _selected_rows,
    _status,
    _wait_for_models,
)

pytestmark = pytest.mark.unit


async def test_config_after_setup_asks_a_new_provider_for_its_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1-F3."""
    monkeypatch.chdir(tmp_path)
    list_models, seen = _hosted_listing()
    runner = _Runner([], by_command=_selected_rows(connection=False))
    app = OnexTui(
        root=_root, runner=runner, load_status=_status, list_models=list_models
    )
    async with app.run_test(size=(140, 50)) as pilot:
        await _open_config(pilot, app)
        screen = app.screen
        assert [e.provider for e in screen._selection.entries] == [
            "openrouter",
            "ollama",
        ]
        screen.query_one("#provider").value = "gemini"
        await pilot.pause()
        key = screen.query_one("#key")
        assert key.display
        assert key.password
        assert not screen.query_one("#url").display
        await _wait_for_models(pilot)
        assert seen == []
        key.value = "gemini-test-key-value"
        await _wait_for_models(pilot)
        assert seen == [("gemini", "gemini-test-key-value")]
