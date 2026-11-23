# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TUI defects from the 2026-11-15 CLI and TUI audit (OMN-23413).

Failure modes:
  U1  a run whose cost was not measured (cost_measured false, or no cost_usd)
      is printed as $0.0000, as if it were free; the backend says a zero cost
      with cost_measured false is unknown (audit S9-1, S8-2). 168 of 261 Beam
      runs on the operator's install had no recorded cost.
  U2  the Claude Code state is worked out again from the plugin step instead
      of read from the top-level claude_code the CLI prints (audit S9-14).
  U3  a non-dict entry in status's observations raises instead of being
      skipped (audit S9-21).
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    OnexTui,
    _hook_presence,
    claude_code_state,
)
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import _root
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui_activity import (
    _answer,
    _Answers,
    _running,
)
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui_cancel import (
    _log,
    _status,
)

pytestmark = pytest.mark.unit


async def _finished(metrics: dict[str, object]) -> tuple[str, str]:
    """The log and the status bar's last-run part after one answered task."""
    runner = _Answers(_answer(metrics))
    app = OnexTui(root=_root, runner=runner, load_status=_status)
    async with app.run_test(size=(140, 40)) as pilot:
        await _running(pilot, app, runner)
        runner.release.set()
        for _ in range(20):
            await pilot.pause(0.05)
        return _log(app), app._last_run


@pytest.mark.parametrize(
    "metrics",
    [
        {"cost_usd": 0.0, "cost_measured": False, "total_tokens": 10},
        {"total_tokens": 10},
        {"cost_usd": None, "total_tokens": 10},
    ],
)
async def test_an_unmeasured_cost_is_not_shown_as_free(
    metrics: dict[str, object],
) -> None:
    """U1."""
    log, last_run = await _finished(metrics)
    assert "$0.0000" not in log
    assert "not measured" in log
    assert "$0.0000" not in last_run
    assert "not measured" in last_run


async def test_a_measured_cost_is_shown() -> None:
    """U1: a priced run keeps its figure, a measured zero too."""
    log, last_run = await _finished({"cost_usd": 0.0042, "cost_measured": True})
    assert "$0.0042" in log
    assert "$0.0042" in last_run
    log, _ = await _finished({"cost_usd": 0.0, "cost_measured": True})
    assert "$0.0000" in log


def test_the_claude_code_state_is_the_clis_own() -> None:
    """U2: the CLI's top-level claude_code wins over the plugin step's guess."""
    document = {
        "claude_code": "signed_out",
        "fix": "Claude Code is not signed in. Run claude and sign in.",
        "observations": [
            {"step": "claude-plugin", "status": "success", "evidence": {}}
        ],
    }
    state, fix = claude_code_state(document)
    assert state == "signed_out"
    assert "sign in" in fix


def test_a_malformed_observation_is_skipped() -> None:
    """U3."""
    document = {
        "observations": [
            "not an observation",
            {"step": "claude-hooks", "evidence": {"prompt_hook": "installed"}},
        ]
    }
    assert _hook_presence(document) == {"prompt_hook": "installed"}
