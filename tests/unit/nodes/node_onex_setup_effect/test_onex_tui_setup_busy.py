# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A Save while another setup runs says so in plain words (OMN-23413).

Seen 2026-11-13 in the operator's /delegation: a first Save's setup was still
installing Claude Code (a ~200 MB download), and the second Save showed
"was not switched on: RuntimeError: A concurrent setup run holds the setup
lock". Failure modes:
  B1  the lock shows as a Python error, with no word on what to do.
  B2  (control) any other failure still shows its own reason.
"""

from __future__ import annotations

import pytest

from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _app,
    _press,
    _Runner,
    _selected_rows,
    _type,
)

pytestmark = pytest.mark.unit

_LOCKED = (
    "Traceback (most recent call last):\n"
    '  File "handler_setup_workflow.py", line 341, in run\n'
    "    ) from None\n"
    "RuntimeError: A concurrent setup run holds the setup lock\n"
)


async def _save_note(errors: str) -> str:
    runner = _Runner(
        [], code=1, errors=errors, by_command=_selected_rows(connection=False)
    )
    app = _app(runner)
    async with app.run_test(size=(140, 44)) as pilot:
        await _type(pilot, "/delegation")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        app.screen.set_all(True)
        await _press(pilot, app, "dsave")
        for _ in range(6):
            await pilot.pause()
        return " ".join(str(app.screen.query_one("#dnote").render()).split())


async def test_a_save_during_another_setup_says_to_wait() -> None:
    """B1."""
    note = await _save_note(_LOCKED)
    assert "RuntimeError" not in note
    assert "another setup is still running" in note
    assert "save again" in note


async def test_another_failure_keeps_its_own_reason() -> None:
    """B2."""
    note = await _save_note("Error: claude is not logged in\n")
    assert "claude is not logged in" in note
    assert "another setup is still running" not in note
