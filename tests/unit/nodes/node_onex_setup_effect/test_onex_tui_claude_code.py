# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""/delegation says whether Claude Code is there before switching it on (OMN-23413).

Operator 2026-11-13: "claude should be already installed, if not the user should
do it separately; we need to detect and inform in the tui itself; if claude is
already installed then we can enable hooks". The tab reads setup's own verify
(`onex delegation status --json`, about 3 s on the real CLI), whose claude-plugin step marks evidence.claude_code.

Failure modes:
  C1  Claude Code is missing, nothing says so, and Save still runs setup.
  C2  a ready Claude Code is not shown as ready.
  C3  a signed-out Claude Code reads as missing, or as ready.
  C4  an installed Claude Code without the ONEX plugin blocks the Save that adds it.
  C5  a check that could not run blocks the Save instead of letting setup decide.
"""

from __future__ import annotations

import json

import pytest
from textual.widgets import Static

from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _app,
    _Runner,
    _selected_rows,
    _type,
)

pytestmark = pytest.mark.unit

_VERIFY = ("delegation", "status")
_ENABLE = ("delegation", "on")


def _verify(status: str, fix: str | None, claude_code: str | None) -> str:
    evidence = {"claude_code": claude_code} if claude_code else {}
    return json.dumps(
        {
            "observations": [
                {"step": "preflight", "status": "success"},
                {
                    "step": "claude-plugin",
                    "status": status,
                    "fix": fix,
                    "evidence": evidence,
                },
            ]
        }
    )


async def _tab(verify: str | None, *, save: bool) -> tuple[str, str, _Runner]:
    rows = {**_selected_rows(connection=False)}
    if verify is not None:
        rows[_VERIFY] = verify
    runner = _Runner([], by_command=rows)
    app = _app(runner)
    async with app.run_test(size=(140, 44)) as pilot:
        await _type(pilot, "/delegation")
        await pilot.press("enter")
        for _ in range(12):
            await pilot.pause()
        line = " ".join(str(app.screen.query_one("#claude", Static).render()).split())
        note = ""
        if save:
            app.screen.set_all(True)
            await pilot.press("ctrl+s")  # no Save button (OMN-26688)
            for _ in range(8):
                await pilot.pause()
            note = " ".join(str(app.screen.query_one("#dnote").render()).split())
        return line, note, runner


async def test_a_missing_claude_code_is_named_and_save_runs_nothing() -> None:
    """C1."""
    line, note, runner = await _tab(
        _verify(
            "unavailable",
            "Claude Code is not installed. Install it yourself (claude.com/claude-code), then repeat this component setup.",
            "not_installed",
        ),
        save=True,
    )
    assert "Claude Code is not installed" in line
    assert "claude.com/claude-code" in line
    assert not any(call[:2] == _ENABLE for call in runner.calls)
    assert not any(call[:2] == ("hooks", "enable") for call in runner.calls)
    assert "Claude Code is not installed" in note


async def test_a_ready_claude_code_is_shown_ready() -> None:
    """C2."""
    line, _, _ = await _tab(_verify("success", None, None), save=False)
    assert "Claude Code: ready" in line


async def test_a_signed_out_claude_code_says_to_sign_in() -> None:
    """C3."""
    line, _, _ = await _tab(
        _verify(
            "action_required",
            "Sign in to Claude Code yourself, then repeat onex setup --action repair.",
            "signed_out",
        ),
        save=False,
    )
    assert "not signed in" in line
    assert "not installed" not in line


async def test_an_installed_claude_code_lets_save_add_the_plugin() -> None:
    """C4."""
    line, _, runner = await _tab(
        _verify(
            "unavailable",
            "Install the selected plugin profile: onex@omninode-tools.",
            None,
        ),
        save=True,
    )
    assert "Claude Code: installed" in line
    assert any(call[:2] == _ENABLE for call in runner.calls)


async def test_a_check_that_could_not_run_does_not_block_save() -> None:
    """C5."""
    line, _, runner = await _tab(None, save=True)
    assert "could not check Claude Code" in line
    assert any(call[:2] == _ENABLE for call in runner.calls)
