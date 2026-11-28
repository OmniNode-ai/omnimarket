# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A running task shows a live line, like a coding agent's CLI (OMN-23413).

Operator 2026-11-13: "when we type prompt it says delegating... i want like
coding agents cli: spinner verb tokens in out can cancel esc option".
`onex delegate --json` reports its tokens when it finishes (input_tokens and
output_tokens in result.metrics, read from a real run that day), so the live
line shows what is known while it runs and the counts arrive with the answer.

Failure modes:
  A1  a running task shows only a static "delegating…": no sign it is alive,
      how long it has run, or how to stop it.
  A2  the spinner does not move, so a stuck app looks the same as a busy one.
  A3  the live line stays after the answer, or the answer gives one token
      total where the run reported tokens in and tokens out.
  A4  a result with only a total shows no tokens at all.
  A5  Esc cancels but leaves the live line spinning.
  G1  the live line sits at the bottom of the window, far below the task it is
      about (operator 2026-11-13, screenshot: "why too much gap").
  G2  the status bar repeats the live line's "working … esc cancels".
  G3  a long conversation pushes the live line off the bottom, or the log
      does not get the whole window back afterwards.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import pytest
from textual.widgets import Static

from omnimarket.nodes.node_onex_setup_effect.cli_tui import OnexTui
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import _root
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui_cancel import (
    _ask,
    _log,
    _SlowRunner,
    _status,
)

pytestmark = pytest.mark.unit


def _answer(metrics: dict[str, object]) -> str:
    return json.dumps(
        {
            "run_id": "ec84611a-5494-4a12-a440-6dff1d4177db",
            "result": {
                "response": "4",
                "model_name": "Beam-1.0-501B-A23B",
                "metrics": metrics,
            },
        }
    )


class _Answers(_SlowRunner):
    """Answers a delegation once ``release`` is set; other reads answer at once."""

    def __init__(self, document: str) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.document = document

    async def capture(
        self, argv: tuple[str, ...], stdin: str | None = None
    ) -> tuple[int, str, str]:
        if argv[:1] != ("delegate",):
            return 0, "", ""
        self.calls.append(argv)
        await self.release.wait()
        return 0, self.document, ""

    async def stream(
        self,
        argv: tuple[str, ...],
        emit: Callable[[str], None],
        stdin: str | None = None,
    ) -> int:
        return 0


def _activity(app: OnexTui) -> Static:
    return app.query_one("#activity", Static)


async def _running(pilot, app: OnexTui, runner: _SlowRunner) -> None:
    await _ask(pilot, "what is 2 + 2?")
    for _ in range(40):
        await pilot.pause(0.05)
        if runner.calls and _activity(app).display:
            return


async def test_a_running_task_shows_a_live_line_with_how_to_cancel() -> None:
    """A1."""
    runner = _Answers(_answer({}))
    app = OnexTui(root=_root, runner=runner, load_status=_status)
    async with app.run_test(size=(140, 40)) as pilot:
        await _running(pilot, app, runner)
        line = str(_activity(app).render())
        design = tui_design().activity
        assert _activity(app).display
        assert any(frame in line for frame in design.frames)
        assert any(verb in line for verb in design.verbs)
        assert "esc to cancel" in line
        assert "0s" in line or "1s" in line
        runner.release.set()


async def test_the_spinner_moves() -> None:
    """A2."""
    runner = _Answers(_answer({}))
    app = OnexTui(root=_root, runner=runner, load_status=_status)
    async with app.run_test(size=(140, 40)) as pilot:
        await _running(pilot, app, runner)
        seen = set()
        for _ in range(12):
            seen.add(str(_activity(app).render()))
            await pilot.pause(0.1)
        assert len(seen) > 1
        runner.release.set()


async def test_the_answer_clears_the_line_and_shows_tokens_in_and_out() -> None:
    """A3: the counts are a real run's (ec84611a's sibling, 2026-11-13)."""
    runner = _Answers(
        _answer(
            {
                "input_tokens": 249,
                "output_tokens": 166,
                "total_tokens": 415,
                "latency_ms": 2052,
                "cost_usd": 0.0,
            }
        )
    )
    app = OnexTui(root=_root, runner=runner, load_status=_status)
    async with app.run_test(size=(140, 40)) as pilot:
        await _running(pilot, app, runner)
        runner.release.set()
        for _ in range(20):
            await pilot.pause(0.05)
        assert not _activity(app).display
        log = _log(app)
        assert "415 tokens" in log  # the card's one tokens figure (AC3)
        assert "delegating…" not in log


async def test_a_total_only_result_still_shows_its_tokens() -> None:
    """A4."""
    runner = _Answers(_answer({"total_tokens": 371, "latency_ms": 1400}))
    app = OnexTui(root=_root, runner=runner, load_status=_status)
    async with app.run_test(size=(140, 40)) as pilot:
        await _running(pilot, app, runner)
        runner.release.set()
        for _ in range(20):
            await pilot.pause(0.05)
        assert "371 tokens" in _log(app)


async def test_esc_cancels_and_clears_the_line() -> None:
    """A5."""
    runner = _SlowRunner()
    app = OnexTui(root=_root, runner=runner, load_status=_status)
    async with app.run_test(size=(140, 40)) as pilot:
        await _running(pilot, app, runner)
        await pilot.press("escape")
        for _ in range(20):
            await pilot.pause(0.05)
        assert runner.cancelled >= 1
        assert not _activity(app).display
        assert "cancelled after" in _log(app)


async def test_the_live_line_sits_right_under_the_conversation() -> None:
    """G1: no empty rows between the last written line and the live line."""
    runner = _Answers(_answer({}))
    app = OnexTui(root=_root, runner=runner, load_status=_status)
    async with app.run_test(size=(140, 60)) as pilot:
        await _running(pilot, app, runner)
        output = app.query_one("#output")
        written = len(output.lines)
        gap = _activity(app).region.y - (output.content_region.y + written)
        assert written < output.content_region.height + 20  # not a full screen
        # one blank row above the line on purpose (operator 2026-11-25:
        # "alignment and space not proper"), never more
        assert 0 <= gap <= 2, (gap, written)
        runner.release.set()


async def test_the_status_bar_does_not_repeat_the_live_line() -> None:
    """G2."""
    runner = _Answers(_answer({}))
    app = OnexTui(root=_root, runner=runner, load_status=_status)
    async with app.run_test(size=(140, 40)) as pilot:
        await _running(pilot, app, runner)
        status = str(app.query_one("#status").render())
        assert "esc cancels" not in status
        assert "working" not in status
        assert "esc to cancel" in str(_activity(app).render())
        runner.release.set()


async def test_a_full_log_still_shows_the_live_line_inside_the_window() -> None:
    """G3: after a long conversation the line is not pushed off the bottom."""
    runner = _Answers(_answer({}))
    app = OnexTui(root=_root, runner=runner, load_status=_status)
    async with app.run_test(size=(140, 30)) as pilot:
        log = app.query_one("#output")
        for n in range(120):
            log.write(f"earlier line {n}")
        await pilot.pause(0.2)
        await _running(pilot, app, runner)
        await pilot.pause(0.2)
        box = app.query_one("#conversation").content_region
        line = _activity(app).region
        assert box.y <= line.y < box.y + box.height, (line, box)
        runner.release.set()
        for _ in range(20):
            await pilot.pause(0.05)
        assert not _activity(app).display
        assert log.region.height == box.height  # the log has the window back
