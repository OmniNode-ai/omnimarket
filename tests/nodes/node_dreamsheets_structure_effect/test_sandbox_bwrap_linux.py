# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the bubblewrap profile actually enforces (Linux with bwrap only).

Each check runs a small probe INSIDE the exact profile the node uses
(``BwrapDreamsheetsLauncher.command_for``), where it must fail, and the same
probe directly on the host as a control, where it must succeed. A check whose
control fails proves nothing, so the control is asserted first.

Set ``ONEX_DREAMSHEETS_CAPABILITY_REPORT=<file>`` to also write the results
as JSON: that file is the Phase 1 capability report.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnimarket.nodes.node_dreamsheets_structure_effect.protocols.sandbox_launcher import (
    BwrapDreamsheetsLauncher,
    RunLayout,
)

BWRAP = shutil.which("bwrap")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        sys.platform != "linux" or BWRAP is None, reason="needs Linux with bubblewrap"
    ),
]

REPORT: dict[str, dict[str, object]] = {}


@pytest.fixture(scope="module", autouse=True)
def write_report() -> Iterator[None]:
    yield
    target = os.environ.get("ONEX_DREAMSHEETS_CAPABILITY_REPORT")
    if target:
        Path(target).write_text(
            json.dumps({"bwrap": BWRAP, "checks": REPORT}, indent=2, sort_keys=True)
        )


@pytest.fixture
def layout(tmp_path: Path) -> RunLayout:
    run = RunLayout(tmp_path / "run")
    run.create()
    run.input_file.write_bytes(b"PK")
    os.chmod(run.input_file, 0o444)
    return run


@pytest.fixture
def launcher(tmp_path: Path) -> BwrapDreamsheetsLauncher:
    root = tmp_path / "squashfs-root"
    root.mkdir()
    assert BWRAP is not None
    return BwrapDreamsheetsLauncher(root, bwrap=BWRAP)


def inside(launcher: BwrapDreamsheetsLauncher, layout: RunLayout, code: str) -> str:
    """Run ``python3 -c code`` in the node's sandbox profile, as the handler spawns it."""
    proc = subprocess.run(
        launcher.command_for(layout, ["/usr/bin/python3", "-c", code]),
        env=launcher.environment(layout),
        capture_output=True,
        text=True,
        timeout=30,
        close_fds=True,
        start_new_session=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def outside(code: str, **kw: object) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30, **kw
    )  # type: ignore[call-overload]
    assert proc.returncode == 0, proc.stderr
    return str(proc.stdout).strip()


def record(name: str, sandboxed: str, control: str, expected_sandboxed: str) -> None:
    REPORT[name] = {
        "sandboxed": sandboxed,
        "control": control,
        "pass": sandboxed == expected_sandboxed,
    }
    assert sandboxed == expected_sandboxed, f"{name}: sandboxed={sandboxed!r}"


def test_no_network(launcher: BwrapDreamsheetsLauncher, layout: RunLayout) -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    probe = (
        "import socket\n"
        "try:\n"
        f"    socket.create_connection(('127.0.0.1', {port}), timeout=3); print('connected')\n"
        "except OSError: print('blocked')"
    )
    try:
        control = outside(probe)
        assert control == "connected"
        record(
            "network_host_loopback", inside(launcher, layout, probe), control, "blocked"
        )
    finally:
        server.close()
    external = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 443), timeout=3); print('connected')\n"
        "except OSError: print('blocked')"
    )
    record("network_external", inside(launcher, layout, external), "n/a", "blocked")


def test_writes_only_under_work(
    launcher: BwrapDreamsheetsLauncher, layout: RunLayout
) -> None:
    probe = (
        "import os\n"
        "ok = []\n"
        "for p in ['/x', '/usr/x', '/in/doc.dsheet', '/config/x', '/opt/ds/x', '/work/tmp/x', '/home/s/x']:\n"
        "    try:\n"
        "        open(p, 'a').close(); ok.append(p)\n"
        "    except OSError: pass\n"
        "print(','.join(ok))"
    )
    record(
        "writable_paths",
        inside(launcher, layout, probe),
        "n/a",
        "/work/tmp/x,/home/s/x",
    )
    assert layout.input_file.read_bytes() == b"PK"


def test_host_tmp_and_home_are_invisible(
    launcher: BwrapDreamsheetsLauncher, layout: RunLayout
) -> None:
    with tempfile.NamedTemporaryFile(prefix="dsprobe-canary-", dir="/tmp") as canary:
        probe = f"import os; print(os.path.exists({canary.name!r}))"
        control = outside(probe)
        assert control == "True"
        record("host_tmp_visible", inside(launcher, layout, probe), control, "False")
    home = "import os; print(os.environ.get('HOME'), sorted(os.listdir(os.environ['HOME'])))"
    record(
        "home_private_and_empty", inside(launcher, layout, home), "n/a", "/home/s []"
    )


def test_environment_is_the_allowlist(
    launcher: BwrapDreamsheetsLauncher,
    layout: RunLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DSPROBE_CANARY_SECRET", "s3cret")
    probe = "import os; print(','.join(sorted(os.environ)))"
    control = outside(probe)
    assert "DSPROBE_CANARY_SECRET" in control
    record(
        "environment",
        inside(launcher, layout, probe),
        control,
        "HOME,LANG,LD_LIBRARY_PATH,PATH,TMPDIR",
    )


def test_no_inherited_handles(
    launcher: BwrapDreamsheetsLauncher, layout: RunLayout, tmp_path: Path
) -> None:
    probe = "import os; print(','.join(sorted(f for f in os.listdir('/proc/self/fd') if int(f) > 2)))"
    with (tmp_path / "canary").open("w") as f:
        fd = f.fileno()
        os.set_inheritable(fd, True)
        control = outside(probe, close_fds=False, pass_fds=(fd,))
        assert str(fd) in control.split(",")
        sandboxed = inside(launcher, layout, probe)
    # listdir itself holds one fd open while listing.
    REPORT["inherited_handles"] = {
        "sandboxed": sandboxed,
        "control": control,
        "pass": str(fd) not in sandboxed.split(","),
    }
    assert str(fd) not in sandboxed.split(",")
    assert len([x for x in sandboxed.split(",") if x]) <= 1


def test_host_processes_are_invisible(
    launcher: BwrapDreamsheetsLauncher, layout: RunLayout
) -> None:
    probe = f"import os; print({os.getpid()} in [int(p) for p in os.listdir('/proc') if p.isdigit()])"
    control = outside(probe)
    assert control == "True"
    record("host_pid_visible", inside(launcher, layout, probe), control, "False")


def test_no_privileges(launcher: BwrapDreamsheetsLauncher, layout: RunLayout) -> None:
    probe = (
        "s = dict(l.split(':', 1) for l in open('/proc/self/status'))\n"
        "print(s['NoNewPrivs'].strip(), s['CapEff'].strip())"
    )
    record("privileges", inside(launcher, layout, probe), "n/a", "1 0000000000000000")
