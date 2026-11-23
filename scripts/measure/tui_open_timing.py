# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Time how long each onex TUI tab takes to open, headless, with the real runner.

For each of /config, /model, /delegation and /dashboard, a fresh TUI starts and
the command is typed at once, as a user does. Two times are printed, from Enter:
`shown` (the tab is the visible screen) and `settled` (every onex command the
action started has finished, so the tab holds its data). Run with a throwaway
HOME to measure a clean install.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from collections.abc import Callable
from typing import Any

from omnimarket.nodes.node_onex_setup_effect.cli_tui import OnexTui, _TabbedScreen
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_onex_command import (
    HandlerOnexCommand,
)

#: Every program the runner started, so "a new onex process per action" is counted.
STARTED: list[tuple[str, ...]] = []
_real_exec = asyncio.create_subprocess_exec


async def _counted_exec(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
    STARTED.append(args)
    return await _real_exec(*args, **kwargs)


vars(asyncio)["create_subprocess_exec"] = _counted_exec


class _Counting(HandlerOnexCommand):
    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.calls: list[tuple[str, ...]] = []

    async def stream(
        self,
        argv: tuple[str, ...],
        emit: Callable[[str], None],
        stdin: str | None = None,
    ) -> int:
        self.active += 1
        self.calls.append(argv)
        try:
            return await super().stream(argv, emit, stdin)
        finally:
            self.active -= 1

    async def capture(
        self, argv: tuple[str, ...], stdin: str | None = None
    ) -> tuple[int, str, str]:
        self.active += 1
        self.calls.append(argv)
        try:
            return await super().capture(argv, stdin)
        finally:
            self.active -= 1


async def open_time(name: str, *, wait_start_up: bool) -> tuple[float, float, int]:
    runner = _Counting()
    app = OnexTui(runner=runner)
    async with app.run_test(size=(140, 45)) as pilot:
        if wait_start_up:
            while runner.active:
                await asyncio.sleep(0.05)
            await pilot.pause()
        before = len(STARTED)
        await pilot.press("slash", *name)
        start = time.monotonic()
        await pilot.press("enter")
        shown = settled = 0.0
        while time.monotonic() - start < 60:
            await pilot.pause(0.02)
            now = time.monotonic() - start
            if not shown and isinstance(app.screen, _TabbedScreen):
                shown = now
            if shown and runner.active == 0:
                settled = now
                break
        return shown, settled, len(STARTED) - before


async def main() -> None:
    """``tui_open_timing.py RUNS`` prints the median of RUNS opens per tab, once
    typed at once and once after the start-up reads; ``tui_open_timing.py cold
    TAB`` prints the first open of TAB in this fresh process (what a user who
    types at once after launching sees)."""
    if sys.argv[1:2] == ["cold"]:
        shown, settled, _started = await open_time(sys.argv[2], wait_start_up=False)
        print(f"/{sys.argv[2]:<10} shown {shown:5.2f}s  settled {settled:5.2f}s")
        return
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    for wait_start_up in (False, True):
        label = "after start-up read" if wait_start_up else "typed at once"
        print(f"-- {label} (median of {runs})")
        for name in ("config", "model", "delegation", "dashboard"):
            rows = [
                await open_time(name, wait_start_up=wait_start_up) for _ in range(runs)
            ]
            print(
                f"/{name:<10} shown {statistics.median(r[0] for r in rows):5.2f}s"
                f"  settled {statistics.median(r[1] for r in rows):5.2f}s"
                f"  onex programs started {rows[0][2]}"
            )


if __name__ == "__main__":
    asyncio.run(main())
