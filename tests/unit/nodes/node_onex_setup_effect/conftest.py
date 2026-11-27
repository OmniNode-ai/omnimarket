# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Share the model-step fixtures (fresh HOME, stub key server, subprocess recorder).

They are defined in the model-step test module; re-exporting them here lets the
other ``onex setup`` test modules in this folder request them by name.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_core.models.event_bus.model_run_node_hosted import ModelRunNodeHosted

from tests.unit.nodes.node_onex_setup_effect.test_onex_setup_model_step import (
    home,
    recorder,
    server,
)

__all__ = ["home", "recorder", "server"]


def _delivery_not_required(node_id: str, state_root: Path) -> None:
    """Stand in for core's delivery check: these tests read the walk's output, not the bus."""
    del node_id, state_root


@pytest.fixture(autouse=True)
def _hosted_run_without_a_hosted_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the walk's tests end without a hosted bus to carry the run's recorded events.

    ``ModelRunNodeHosted.run`` refuses a finished run whose recorded events no forwarder
    delivered. These tests drive the whole ``onex setup`` walk on a throwaway HOME and read
    its output; no hosted bus or forwarder overlay exists in the test environment, so only
    that delivery check is replaced and the run itself stays real.
    """
    monkeypatch.setattr(
        ModelRunNodeHosted, "require_delivered", staticmethod(_delivery_not_required)
    )


@pytest.fixture(autouse=True)
def _tui_state_in_a_throwaway_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The TUI remembers its theme and launch count under the home folder; a test
    must neither read what an earlier run saved nor leave its own behind."""
    from omnimarket.nodes.node_onex_setup_effect import cli_tui

    monkeypatch.setattr(cli_tui.ThemeStore, "_path", tmp_path / "tui-theme")
    monkeypatch.setattr(cli_tui.LaunchStore, "_path", tmp_path / "tui-launches")
    monkeypatch.setattr(cli_tui.StatusStore, "_path", tmp_path / "tui-status.json")
