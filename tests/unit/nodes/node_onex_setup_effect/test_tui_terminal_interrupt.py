# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An interrupt at a child CLI prompt returns control to the terminal UI."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _app_main() -> None:
    import fcntl
    import termios

    from omnimarket.nodes.node_onex_setup_effect.cli_tui import OnexTui
    from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
        _index,
        _root,
        _Runner,
        _status,
    )

    # A session leader with a controlling terminal receives keyboard interrupts.
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    class Runner(_Runner):
        def in_terminal(self, argv: tuple[str, ...]) -> int:
            assert argv == ("setup",)
            return subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import click; click.command()(lambda: click.confirm('Approve test setup', abort=True))()",
                ],
                check=False,
            ).returncode

    app = OnexTui(
        root=_root,
        runner=Runner(["Not a TUI command"]),
        load_status=_status,
        load_index=_index,
    )
    app.call_after_refresh(app.open_tab, "config")
    app.run()


def _expect(fd: int, text: bytes, timeout: float = 25, *, fail: bool = True) -> bool:
    received = bytearray()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        readable, _, _ = select.select([fd], [], [], 0.2)
        if readable:
            try:
                part = os.read(fd, 65536)
            except OSError:
                break
            if not part:
                break
            received.extend(part)
            if text in received:
                return True
    if fail:
        pytest.fail(f"Terminal never showed {text!r}. Last output: {received[-200:]!r}")
    return False


@pytest.mark.skipif(os.name != "posix", reason="requires a POSIX controlling terminal")
def test_ctrl_c_at_guided_setup_keeps_the_tui_usable(tmp_path: Path) -> None:
    import fcntl
    import struct
    import termios

    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    app = subprocess.Popen(
        [sys.executable, "-m", __name__],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
        env={**os.environ, "HOME": str(tmp_path), "TERM": "xterm-256color"},
    )
    os.close(slave)
    try:
        _expect(master, b"Guided setup")
        os.write(master, b"\x07")
        _expect(master, b"Approve test setup")
        os.write(master, b"\x03")
        _expect(master, b"Guided setup did not finish")
        # The message shows (in a notice) while the tab still reads the setup
        # again, and the tab refuses Esc until that ends, so press it until
        # the main screen's prompt is back.
        for _ in range(40):
            os.write(master, b"\x1b")
            if _expect(master, b"Ask onex", timeout=0.5, fail=False):
                break
        else:
            pytest.fail("Esc never closed /config after guided setup stopped")
        time.sleep(0.25)
        for key in b"/doctor\r":
            os.write(master, bytes([key]))
            time.sleep(0.02)
        _expect(master, b"Not a TUI command")
    finally:
        if app.poll() is None:
            os.killpg(app.pid, signal.SIGTERM)
        app.wait(timeout=5)
        os.close(master)


if __name__ == "__main__":
    _app_main()
