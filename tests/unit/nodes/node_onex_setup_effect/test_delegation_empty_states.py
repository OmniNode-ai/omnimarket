"""OMN-26620: the Delegation tab on a new install (real-terminal sweep,
2026-11-25, fresh home, Claude Code installed but signed out).

  D1  "not signed in" is said twice: the tab's own line, then the CLI's
      sentence that repeats it.
  D2  with no model set up, the "Tried in this order" box is an empty frame
      seven rows tall, above the note that says what to do.
  D3  once a model is set up the order box is back.
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

pytestmark = pytest.mark.timeout(60)

# The real `onex delegation status --json` fix on a signed-out Claude Code
# (captured 2026-11-25 in the fresh-home sweep).
_REAL_FIX = (
    "Claude Code is not signed in. Run claude and sign in, then run onex "
    "delegation on again."
)


def _verify_signed_out() -> str:
    return json.dumps(
        {
            "observations": [
                {"step": "preflight", "status": "success"},
                {
                    "step": "claude-plugin",
                    "status": "action_required",
                    "fix": _REAL_FIX,
                    "evidence": {"claude_code": "signed_out"},
                },
            ]
        }
    )


async def _delegation(rows: dict[tuple[str, ...], str]):
    app = _app(_Runner([], by_command=rows))
    async with app.run_test(size=(120, 36)) as pilot:
        await _type(pilot, "/delegation")
        await pilot.press("enter")
        for _ in range(12):
            await pilot.pause()
        screen = app.screen
        claude = " ".join(str(screen.query_one("#claude", Static).render()).split())
        note = " ".join(str(screen.query_one("#dnote", Static).render()).split())
        return claude, note, screen.query_one("#selected").display


async def test_signed_out_is_said_once() -> None:
    """D1."""
    rows = {
        **_selected_rows(connection=False),
        ("delegation", "status"): _verify_signed_out(),
    }
    claude, _, _ = await _delegation(rows)
    assert claude.lower().count("signed in") == 1, claude
    assert "sign in" in claude, claude


async def test_no_model_hides_the_empty_order_box() -> None:
    """D2."""
    _, note, shown = await _delegation({("models", "list"): ""})
    assert not shown
    assert "Config tab" in note, note


async def test_a_set_up_model_shows_the_order_box() -> None:
    """D3."""
    _, note, shown = await _delegation(_selected_rows(connection=False))
    assert shown
    assert "Config tab" not in note, note
