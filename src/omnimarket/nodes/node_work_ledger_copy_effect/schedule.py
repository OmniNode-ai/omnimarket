# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The copy's hourly launchd agent: render, install and uninstall.

The cadence is the contract's ``schedule`` block; the interpreter, environment and log directory are
the overlay's ``launchd`` block. ``uninstall`` is the rollback: it boots the agent out and removes
the plist, and changes nothing else.
"""

from __future__ import annotations

import plistlib
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import yaml

from omnimarket.nodes.node_work_ledger_copy_effect.adapters.adapters_real import (
    expand_home,
)
from omnimarket.nodes.node_work_ledger_copy_effect.models import ModelCopyOverlay

NODE_MODULE = "omnimarket.nodes.node_work_ledger_copy_effect"
CONTRACT_PATH = Path(__file__).resolve().parent / "contract.yaml"

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(command), capture_output=True, text=True, check=False)


def load_schedule(path: Path = CONTRACT_PATH) -> dict[str, object]:
    block = yaml.safe_load(path.read_text(encoding="utf-8"))["schedule"]
    return {str(key): value for key, value in block.items()}


def render_plist(overlay: ModelCopyOverlay, schedule: dict[str, object]) -> bytes:
    launchd = overlay.launchd
    minute = int(str(schedule["minute"]))
    every = int(str(schedule["every_hours"]))
    calendar = [{"Hour": hour, "Minute": minute} for hour in range(0, 24, every)]
    return plistlib.dumps(
        {
            "Label": str(schedule["unit_name"]),
            "ProgramArguments": [
                expand_home(launchd.interpreter),
                "-m",
                NODE_MODULE,
                "run",
            ],
            "WorkingDirectory": expand_home(launchd.working_directory),
            "EnvironmentVariables": {
                key: expand_home(value) for key, value in launchd.environment.items()
            },
            "StartCalendarInterval": calendar,
            "RunAtLoad": False,
            "ExitTimeOut": launchd.max_runtime_seconds,
            "StandardOutPath": f"{expand_home(launchd.log_directory)}/work-ledger-copy.out.log",
            "StandardErrorPath": f"{expand_home(launchd.log_directory)}/work-ledger-copy.err.log",
        }
    )


def install(
    overlay: ModelCopyOverlay,
    schedule: dict[str, object],
    *,
    agents_dir: Path,
    uid: int,
    run: Runner = _run,
) -> int:
    Path(expand_home(overlay.launchd.log_directory)).mkdir(parents=True, exist_ok=True)
    agents_dir.mkdir(parents=True, exist_ok=True)
    target = agents_dir / f"{schedule['unit_name']}.plist"
    target.write_bytes(render_plist(overlay, schedule))
    domain = f"gui/{uid}"
    run(["launchctl", "bootout", f"{domain}/{schedule['unit_name']}"])
    return run(["launchctl", "bootstrap", domain, str(target)]).returncode


def uninstall(
    schedule: dict[str, object], *, agents_dir: Path, uid: int, run: Runner = _run
) -> int:
    run(["launchctl", "bootout", f"gui/{uid}/{schedule['unit_name']}"])
    (agents_dir / f"{schedule['unit_name']}.plist").unlink(missing_ok=True)
    return 0
