"""OMN-26620: the status line at 80 columns after a task (end-to-end pass
2026-11-26): it read "... last run Qwen3.8-27B $0.0000 · run" and stopped
mid-part, because the last run was added whether it fitted or not.

  S1  a part of the status line is cut instead of shortened or left out.
"""

from __future__ import annotations

import pytest

from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import _app, _Runner

pytestmark = pytest.mark.timeout(60)

_SHORT = "last run Qwen3.8-27B on a-long-server-name:8000 $0.0000"
_LAST = f"{_SHORT} · run f2cdecb0"


@pytest.mark.parametrize("width", [80, 100, 160])
async def test_the_last_run_shows_whole_shortened_or_not_at_all(width: int) -> None:
    """S1."""
    app = _app(_Runner([]))
    async with app.run_test(size=(width, 30)) as pilot:
        for _ in range(4):
            await pilot.pause()
        app._last_run = _LAST
        app._last_run_short = _SHORT
        app._render_status()
        await pilot.pause()
        status = app._main().query_one("#status")
        line = str(status.render())
        assert len(line) <= width - 2, (len(line), line)
        # whole, or without the run id, or not at all; never cut mid-part
        assert (
            _LAST in line
            or (_SHORT in line and "f2cdecb0" not in line)
            or "last run" not in line
        ), line
        if width >= 160:
            assert _LAST in line, line
