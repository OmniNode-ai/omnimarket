# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""--no-interactive must refuse a missing key even when stdin is a real TTY."""

import os
import pty
import signal
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


def test_unattended_setup_never_opens_a_hidden_terminal_prompt(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [
            str(Path(sys.executable).parent / "onex"),
            "setup",
            "--action",
            "install",
            "--provider",
            "openai",
            "--model",
            "fixture-model",
            "--yes",
            "--no-interactive",
            "--workspace",
            str(tmp_path / "workspace"),
            "--json",
        ],
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        cwd=tmp_path,
        start_new_session=True,
    )
    os.close(slave)
    try:
        try:
            stdout, stderr = process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate(timeout=5)
            pytest.fail(
                "--no-interactive waited for terminal input: " + stderr.decode()[-500:]
            )
        assert process.returncode != 0
        assert b"Paste your" not in stdout + stderr
        assert not list(home.iterdir())
        assert not (tmp_path / "workspace").exists()
    finally:
        os.close(master)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
