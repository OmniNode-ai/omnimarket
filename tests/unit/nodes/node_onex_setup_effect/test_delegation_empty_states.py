"""OMN-26620: the Delegation tab on a new install (real-terminal sweep,
2026-11-25, fresh home, Claude Code installed but signed out).

  D1  "not signed in" is said twice: the tab's own line, then the CLI's
      sentence that repeats it.
  D2  with no model set up, the note does not say what to do (the tab has
      no models table since OMN-26688: the Model tab lists them).
  D3  once a model is set up the note still says to add one.
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


async def _delegation(rows: dict[tuple[str, ...], str]):  # type: ignore[no-untyped-def]
    app = _app(_Runner([], by_command=rows))
    async with app.run_test(size=(120, 36)) as pilot:
        await _type(pilot, "/delegation")
        await pilot.press("enter")
        for _ in range(12):
            await pilot.pause()
        screen = app.screen
        claude = " ".join(str(screen.query_one("#claude", Static).render()).split())
        note = " ".join(str(screen.query_one("#dnote", Static).render()).split())
        return claude, note, len(screen.query("ModelTable"))


async def test_signed_out_is_said_once() -> None:
    """D1."""
    rows = {
        **_selected_rows(connection=False),
        ("delegation", "status"): _verify_signed_out(),
    }
    claude, _, _ = await _delegation(rows)
    assert claude.lower().count("signed in") == 1, claude
    assert "sign in" in claude, claude


async def test_no_model_says_what_to_do() -> None:
    """D2."""
    _, note, tables = await _delegation({("models", "list"): ""})
    assert tables == 0
    assert "Config tab" in note, note


async def test_a_set_up_model_needs_no_note() -> None:
    """D3."""
    _, note, tables = await _delegation(_selected_rows(connection=False))
    assert tables == 0
    assert "Config tab" not in note, note
