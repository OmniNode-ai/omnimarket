# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The registry module tick template's arguments and the unit heartbeat (OMN-23662).

The host installer renders every ``registry_module_tick`` unit as ``python -m <module> --omni-home
<root> --state-dir <dir>``, so a tick main that accepts only ``--probe`` exits 2 on every scheduled
run. Each tick main adds the template's two arguments here (optional, so ``--probe`` and a bare run
keep working) and writes ``last-run.json`` into the state directory after the pass: one JSON line
recording when the unit ran, with what exit code and what result, so the installer can tell a
ticking unit from a silent one. The probe writes nothing.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

HEARTBEAT_FILE = "last-run.json"


def add_tick_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the registry module tick template's two arguments, both optional."""
    parser.add_argument(
        "--omni-home",
        type=Path,
        default=None,
        help=(
            "registry root the unit renders; accepted for the template, "
            "the overlay names every path"
        ),
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="the unit's evidence directory where last-run.json is written",
    )


def write_tick_heartbeat(
    state_dir: Path | None,
    *,
    code: int,
    line: dict[str, object],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Write the unit heartbeat atomically; silent when the run carried no state directory."""
    if state_dir is None:
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / HEARTBEAT_FILE
    held = state_dir / (HEARTBEAT_FILE + ".tmp")
    held.write_text(
        json.dumps({"ran_at": now().isoformat(), "exit_code": code, "result": line}),
        encoding="utf-8",
    )
    held.replace(target)
