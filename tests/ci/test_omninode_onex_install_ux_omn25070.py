# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How the customer installer looks and behaves for a person (OMN-25070).

Operator ruling, 2026-11-25: the install should be fast, with spinners, interactive and
modern, with all the details clear. Each test pipes the rendered script into
bash, as `curl -fsSL .../install.sh | bash` does, against a stub uv, so no
network is used.

Failure modes each test catches:
  X1  the plan names the wrong folder for onex (uv's bin folder follows
      UV_TOOL_BIN_DIR, then XDG_BIN_HOME, then ~/.local/bin);
  X2  a prompt that waits for an answer hangs an install with no terminal
      (CI, a piped log);
  X3  uv's own output floods the screen and hides the result;
  X4  a failed install hides its cause;
  X5  spinner frames or colour codes leak into a log when there is no terminal;
  X6  a person at a terminal sees no spinner and is not asked before anything
      changes;
  X7  answering anything but yes still installs something;
  X8  --yes or ONEX_INSTALL_YES=1 still asks;
  X9  a reinstall or an upgrade is reported as a fresh install;
  X10 the first launch compiles every module, so it is twice as slow;
  X11 the PATH advice is a long absolute path, or a line that does not work
      when pasted into the shell.
"""

from __future__ import annotations

import os
import pty
import select
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tests.ci.test_omninode_onex_install_sh_omn25070 import (
    SCRIPT_TEXT,
    _native_target,
    _run,
    _script_value,
    _stub,
    _toolbox,
)

VERSION = _script_value("ONEX_VERSION")
PACKAGE = _script_value("ONEX_PACKAGE")

# A uv recent enough to use. `tool install` writes an onex that answers
# --version, logs its arguments, and chatters the way uv does.
_FAKE_UV = r"""
case "$1" in
    --version) echo "uv 0.9.0" ;;
    tool)
        case "$2" in
            dir) echo "$UV_TOOL_BIN_DIR" ;;
            list)
                if [ -f "$HOME/installed-version" ]; then
                    echo "omninode-onex v$(cat "$HOME/installed-version")"
                    echo "- onex"
                fi
                ;;
            install)
                echo "$*" >> "$HOME/uv-args"
                echo "Resolved 180 packages in 261ms"
                [ -n "${FAKE_UV_SLOW:-}" ] && sleep "$FAKE_UV_SLOW"
                if [ -n "${FAKE_UV_FAIL:-}" ]; then
                    echo "error: boom, no matching distribution" >&2
                    exit 2
                fi
                mkdir -p "$UV_TOOL_BIN_DIR"
                printf '#!/bin/sh\necho "onex @VERSION@"\n' > "$UV_TOOL_BIN_DIR/onex"
                chmod 755 "$UV_TOOL_BIN_DIR/onex"
                if [ -f "$HOME/installed-version" ] && [ "$(cat "$HOME/installed-version")" = "@VERSION@" ]; then
                    echo "\`omninode-onex==@VERSION@\` is already installed"
                fi
                ;;
        esac
        ;;
esac
""".replace("@VERSION@", VERSION)


def _setup(
    tmp_path: Path, *, bin_dir: Path | None = None
) -> tuple[dict[str, str], Path]:
    if _native_target() == "x86_64-apple-darwin":
        pytest.skip("Intel Macs are not supported yet; the installer refuses first")
    tools = _toolbox(tmp_path / "tools")
    _stub(tools, "uv", _FAKE_UV)
    home = tmp_path / "home"
    home.mkdir()
    target_bin = bin_dir or (tmp_path / "bin")
    env = {
        "HOME": str(home),
        "PATH": str(tools),
        "UV_TOOL_BIN_DIR": str(target_bin),
        "LANG": "en_US.UTF-8",
    }
    return env, home


def _log(home: Path) -> Path:
    return home / ".local" / "state" / "omninode" / "install.log"


@pytest.mark.unit
def test_the_plan_names_the_exact_folder_before_anything_changes(
    tmp_path: Path,
) -> None:
    """X1."""
    env, _ = _setup(tmp_path)
    result = _run({**env, "ONEX_INSTALL_DRY_RUN": "1"})
    assert result.returncode == 0, result.stderr
    assert f"{tmp_path / 'bin'}/onex" in result.stderr, result.stderr
    assert f"{PACKAGE} {VERSION}" in result.stderr
    assert "No sudo" in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    ("variables", "expected"),
    [
        ({}, "~/.local/bin/onex"),
        ({"XDG_BIN_HOME": "/opt/where/bin"}, "/opt/where/bin/onex"),
    ],
)
def test_the_plan_follows_uvs_own_bin_folder_rule(
    tmp_path: Path, variables: dict[str, str], expected: str
) -> None:
    """X1: without UV_TOOL_BIN_DIR, uv uses XDG_BIN_HOME, then ~/.local/bin."""
    env, _ = _setup(tmp_path)
    del env["UV_TOOL_BIN_DIR"]
    result = _run({**env, **variables, "ONEX_INSTALL_DRY_RUN": "1"})
    assert result.returncode == 0, result.stderr
    assert expected in result.stderr, result.stderr


@pytest.mark.unit
def test_an_install_without_a_terminal_never_waits(tmp_path: Path) -> None:
    """X2, X3, X5."""
    env, home = _setup(tmp_path)
    result = _run(env, timeout=60)
    assert result.returncode == 0, result.stderr
    out = result.stderr
    for step in ("[1/3]", "[2/3]", "[3/3]"):
        assert step in out, out
    assert f"Installed {PACKAGE}=={VERSION}" in out, out
    assert "Resolved 180 packages" not in out, out
    assert "Resolved 180 packages" in _log(home).read_text()
    assert "\x1b" not in out, repr(out)
    assert "\r" not in out, repr(out)
    assert "Press" not in out
    assert "command not found" not in out, out


@pytest.mark.unit
def test_a_failed_install_shows_its_cause_and_the_log(tmp_path: Path) -> None:
    """X4."""
    env, home = _setup(tmp_path)
    result = _run({**env, "FAKE_UV_FAIL": "1"}, timeout=60)
    assert result.returncode == 1, result.stderr
    assert "boom, no matching distribution" in result.stderr, result.stderr
    assert "command not found" not in result.stderr, result.stderr
    assert (
        str(_log(home)) in result.stderr
        or "~/.local/state/omninode/install.log" in result.stderr
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("before", "expected"),
    [
        (None, f"Installed {PACKAGE}=={VERSION}"),
        (VERSION, "already installed"),
        ("0.0.0", f"Upgraded from 0.0.0 to {PACKAGE}=={VERSION}"),
    ],
)
def test_reinstall_and_upgrade_are_named(
    tmp_path: Path, before: str | None, expected: str
) -> None:
    """X9."""
    env, home = _setup(tmp_path)
    if before is not None:
        (home / "installed-version").write_text(before)
        bin_dir = Path(env["UV_TOOL_BIN_DIR"])
        bin_dir.mkdir()
        (bin_dir / "onex").write_text("#!/bin/sh\necho onex\n")
        (bin_dir / "onex").chmod(0o755)
    result = _run(env, timeout=60)
    assert result.returncode == 0, result.stderr
    assert expected in result.stderr, result.stderr


def _in_terminal(
    env: dict[str, str], answer: bytes | None, timeout: float = 60
) -> tuple[int, str]:
    """Run `cat script | bash` with a terminal as the controlling tty, the way a
    person runs the curl line; answer the question when it is asked."""
    script = Path(env["HOME"]) / "install.sh"
    script.write_text(SCRIPT_TEXT)
    pid, fd = pty.fork()
    if pid == 0:  # the child: a terminal session of its own
        os.execve(
            shutil.which("bash") or "/bin/bash",
            ["bash", "-c", f"cat '{script}' | bash"],
            {**env, "TERM": "xterm-256color"},
        )
    seen = b""
    answered = False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            seen += chunk
        if answer is not None and not answered and b"Press" in seen:
            os.write(fd, answer)
            answered = True
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), seen.decode("utf-8", "replace")


_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


@pytest.mark.unit
def test_a_person_is_asked_first_and_sees_a_spinner(tmp_path: Path) -> None:
    """X6."""
    env, _ = _setup(tmp_path)
    code, seen = _in_terminal({**env, "FAKE_UV_SLOW": "1"}, b"\r")
    assert code == 0, seen
    assert seen.index("Press") < seen.index("[1/3]"), seen
    assert any(frame in seen for frame in _FRAMES), seen
    assert f"Installed {PACKAGE}=={VERSION}" in seen, seen
    assert "command not found" not in seen, seen


@pytest.mark.unit
def test_answering_no_changes_nothing(tmp_path: Path) -> None:
    """X7."""
    env, home = _setup(tmp_path)
    code, seen = _in_terminal(env, b"n\r")
    assert code == 1, seen
    assert "Nothing was changed" in seen, seen
    assert not (home / "uv-args").exists()
    assert not (Path(env["UV_TOOL_BIN_DIR"]) / "onex").exists()


@pytest.mark.unit
def test_yes_skips_the_question(tmp_path: Path) -> None:
    """X8."""
    env, _ = _setup(tmp_path)
    code, seen = _in_terminal({**env, "ONEX_INSTALL_YES": "1"}, None)
    assert code == 0, seen
    assert "Press" not in seen, seen


@pytest.mark.unit
def test_bytecode_is_compiled_at_install_so_the_first_launch_is_fast(
    tmp_path: Path,
) -> None:
    """X10: without it the first `onex --help` took 8.80 s against 4.01 s
    compiled (NUC, 2026-11-25, uv 0.9 with a warm cache)."""
    env, home = _setup(tmp_path)
    result = _run(env, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "--compile-bytecode" in (home / "uv-args").read_text()


@pytest.mark.unit
def test_the_path_advice_is_short_and_works_when_pasted(tmp_path: Path) -> None:
    """X11."""
    env, home = _setup(tmp_path)
    bin_dir = home / ".local" / "bin"
    env = {**env, "UV_TOOL_BIN_DIR": str(bin_dir), "SHELL": "/bin/zsh"}
    result = _run(env, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "~/.local/bin is not on your PATH yet" in result.stderr, result.stderr
    assert "Add this line to ~/.zshrc" in result.stderr, result.stderr
    line = next(
        text.strip()
        for text in result.stderr.splitlines()
        if text.strip().startswith("export PATH=")
    )
    assert line == 'export PATH="$HOME/.local/bin:$PATH"', line
    pasted = subprocess.run(
        ["/bin/bash", "-c", f'{line}; printf "%s" "$PATH"'],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert pasted.stdout.split(":")[0] == str(bin_dir), pasted.stdout
