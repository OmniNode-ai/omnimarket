# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A host home for the lab ssh probe tests: the rendered block runs as a lab host runs it, under /bin/sh with a stub ssh."""

from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path

from omnimarket.nodes.node_lab_ssh_probe_compute.handlers import HandlerLabSshProbe
from omnimarket.nodes.node_lab_ssh_probe_compute.models import (
    ModelLabSshHostRow,
    ModelLabSshProbeRequest,
    ModelLabSshProbeScript,
)

CACHE_DIR = "cache"
PATTERN = r"^[A-Za-z0-9][A-Za-z0-9@._-]*$"
BOUND_SH = 'probe_bound() { shift; "$@"; }'
# The stub ssh answers only for the destinations named in ANSWERS (a space separated list); the destination is the argument
# before the command ``true``.
SSH_STUB = (
    "#!/bin/sh\n"
    'dest=""; prev=""\n'
    'for a in "$@"; do if [ "$a" = true ]; then dest="$prev"; fi; prev="$a"; done\n'
    'for ok in $ANSWERS; do [ "$ok" = "$dest" ] && exit 0; done\n'
    "exit 255\n"
)

H202_ROWS = (
    ModelLabSshHostRow(name="h202", target="h202-addr"),
    ModelLabSshHostRow(name="h051", target="h051", fallback_targets=("hp-gen8",)),
    ModelLabSshHostRow(name="h053", target="h053", fallback_targets=("hp-gen9",)),
)


def row(
    name: str, target: str, *fallbacks: str, available: bool = True
) -> ModelLabSshHostRow:
    return ModelLabSshHostRow(
        name=name, target=target, fallback_targets=fallbacks, available=available
    )


def request(
    host: str, rows: tuple[ModelLabSshHostRow, ...], *, check_s: int = 5
) -> ModelLabSshProbeRequest:
    return ModelLabSshProbeRequest(
        host=host,
        rows=rows,
        bound_sh=BOUND_SH,
        path_export='export PATH="$HOME/bin:$PATH"',
        cache_dir=CACHE_DIR,
        check_s=check_s,
        connect_s=2,
        fresh_min=10,
        lock_min=3,
        destination_pattern=PATTERN,
    )


def render(host: str, rows: tuple[ModelLabSshHostRow, ...]) -> ModelLabSshProbeScript:
    return HandlerLabSshProbe().handle(request(host, rows))


def home(answers: tuple[str, ...]) -> tuple[Path, dict[str, str]]:
    """A short-path host home holding the stub ssh, and the environment the probe runs in."""
    top = Path(tempfile.mkdtemp(prefix="ls"))
    (top / "bin").mkdir()
    stub = top / "bin" / "ssh"
    stub.write_text(SSH_STUB, encoding="utf-8")
    stub.chmod(0o755)
    env = {
        "HOME": str(top),
        "PATH": f"{top}/bin:/usr/bin:/bin",
        "ANSWERS": " ".join(answers),
    }
    return top, env


def run_probe(script: str, top: Path, env: dict[str, str]) -> str:
    """The PARITY-CAP line the second run prints, once the detached refresh of the first run wrote its verdict."""
    first = subprocess.run(
        ["/bin/sh", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert first.stdout.strip().endswith("unknown not-read")
    cache = top / CACHE_DIR
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        done = [
            p
            for p in cache.glob("lab-ssh-*")
            if p.suffix not in (".lock", ".tmp") and p.is_file()
        ]
        if done and not list(cache.glob("*.lock")):
            break
        time.sleep(0.05)
    second = subprocess.run(
        ["/bin/sh", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return second.stdout.strip().splitlines()[-1]
