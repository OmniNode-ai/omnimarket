"""OMN-26620: the /doctor page reads as one clean page in a real terminal.

Seen on a real 140x40 run: the title row wrapped its summary onto a second row, the
raw command echo and an "exit 1" line sat under the page, and one check showed its
raw name (env_vars). Each failure mode below was red on the previous head.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from omnimarket.nodes.node_onex_setup_effect.cli_tui import ModelTuiStatus, OnexTui
from omnimarket.nodes.node_onex_setup_effect.doctor_page import render_doctor_page

_REPORT = {
    "total": 14,
    "passed": 2,
    "failed": 4,
    "checks": [
        {"name": "Docker", "status": "healthy", "message": "Docker daemon is running"},
        {"name": "env_vars", "status": "unhealthy", "message": "Missing FOO_KEY"},
        {"name": "Node.js version", "status": "unhealthy", "message": "bad: v1"},
        {"name": "Linear API", "status": "unknown", "message": "not set up"},
    ],
}


class _DoctorRunner:
    """Answers doctor with a report that has failures, as the real command does (exit 1)."""

    async def capture(
        self, argv: tuple[str, ...], stdin: str | None = None
    ) -> tuple[int, str, str]:
        return 1, json.dumps(_REPORT), ""

    async def stream(
        self,
        argv: tuple[str, ...],
        emit: Callable[[str], None],
        stdin: str | None = None,
    ) -> int:
        emit(json.dumps(_REPORT))
        return 1


async def _ready() -> ModelTuiStatus:
    return ModelTuiStatus(model="Qwen3.8-27B", delegation="off", models=1)


async def _doctor_screen(width: int, height: int = 40) -> list[str]:
    app = OnexTui(load_status=_ready, runner=_DoctorRunner())
    async with app.run_test(size=(width, height)) as pilot:
        await pilot.pause()
        await pilot.press(*"/doctor", "enter")
        await pilot.pause()
        await pilot.pause()
        return [
            "".join(segment.text for segment in strip).rstrip()
            for strip in app.screen._compositor.render_strips()
        ]


async def test_the_title_and_its_summary_share_one_row_at_every_width() -> None:
    for width in (80, 100, 140):
        rows = await _doctor_screen(width)
        title_rows = [row for row in rows if "Setup check" in row]
        assert len(title_rows) == 1, (width, rows)
        assert "of 14" in title_rows[0], (width, title_rows[0])


async def test_no_raw_command_echo_or_exit_line_under_the_page() -> None:
    shown = "\n".join(await _doctor_screen(140))
    assert "$ onex doctor" not in shown
    assert "exit 1" not in shown


async def test_a_check_shows_a_plain_name_not_the_raw_one() -> None:
    shown = "\n".join(await _doctor_screen(140))
    assert "env_vars" not in shown
    assert "Env vars" in shown


def test_a_problem_row_does_not_say_needs_attention_twice() -> None:
    rows = render_doctor_page(_REPORT, width=100)
    problems = [row for row in rows if row.startswith("!")]
    assert problems
    for row in problems:
        assert row.lower().count("needs attention") == 1, row
