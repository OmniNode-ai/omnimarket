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


def _expect(fd: int, text: bytes) -> None:
    received = bytearray()
    end = time.monotonic() + 25
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
                return
    pytest.fail(f"Terminal never showed {text!r}. Last output: {received[-200:]!r}")


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
        _expect(master, b"Guided setup exited with code 1")
        os.write(master, b"\x1b")
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
