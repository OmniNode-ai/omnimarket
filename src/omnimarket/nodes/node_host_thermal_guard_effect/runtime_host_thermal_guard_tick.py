# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Registry module tick main for the host thermal guard (OMN-24562).

    python -m omnimarket.nodes.node_host_thermal_guard_effect.runtime_host_thermal_guard_tick --omni-home <root> --state-dir <dir>

The host installer renders every ``registry_module_tick`` unit as that command (the unit's own state directory holds the
episode file and ``last-run.json``, the unit heartbeat). ``--probe`` exits 0 after one status line without loading an overlay or
touching a file. ``--simulate-readings 90,70`` runs the guard over those hottest readings (the clock advancing past the minimum
hold each step) against a throwaway hold file and an in-memory ledger, and prints each step's rows and hold state: a dry run of a
hold and a release for a host that is not hot. Exit 0: nothing left undone. 3: a ledger row was not delivered or refused, or a
file could not be used. 2: the overlay is refused, the host is unnamed or unlisted, or the append command is not set.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.tick_unit_arguments import (
    add_tick_arguments,
    write_tick_heartbeat,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    CommandLedgerAppender,
)

from .adapters import (
    FileEpisodeStore,
    FileHoldStore,
    HostLaneCutter,
    HostThermalSensors,
    InMemoryLaneCutter,
    InMemoryLedgerAppender,
    InMemorySensors,
)
from .handlers import HandlerHostThermalGuard
from .models import ModelHostThermalGuardRequest, ModelThermalReading
from .overlay import ModelHostThermalGuardOverlay, load_overlay

PROBE_FLAG = "--probe"
PROBE_STATUS = "probe_ok"
EXIT_OK, EXIT_UNDONE, EXIT_REFUSED = 0, 3, 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omnimarket.nodes.node_host_thermal_guard_effect.runtime_host_thermal_guard_tick",
        description="One pass of the host thermal guard",
    )
    parser.add_argument(
        PROBE_FLAG,
        action="store_true",
        help="Start check: exit 0 and print one status line without loading an overlay or writing a file",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="decide and write, cut and send nothing"
    )
    parser.add_argument(
        "--simulate-readings",
        default=None,
        help="comma-separated hottest readings in C: run the guard over them against a throwaway hold file and an in-memory ledger",
    )
    parser.add_argument("--host", default=None, help="this host's name in the overlay")
    add_tick_arguments(parser)
    return parser


def _refuse(args: argparse.Namespace, detail: str) -> int:
    line: dict[str, object] = {"status": "refused", "detail": detail}
    if args.state_dir is not None and not args.simulate_readings:
        write_tick_heartbeat(args.state_dir, code=EXIT_REFUSED, line=line)
    sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
    return EXIT_REFUSED


def simulate(
    overlay: ModelHostThermalGuardOverlay, host: str, readings: list[float]
) -> int:
    """Each reading is one run, the clock stepping past the minimum hold; nothing outside a temporary directory is written."""
    start = datetime.now(UTC)
    clock = [start]
    ledger = InMemoryLedgerAppender()
    code = EXIT_OK
    with tempfile.TemporaryDirectory(prefix="thermal-guard-simulation-") as scratch:
        holds = FileHoldStore(Path(scratch), overlay.hold_file)
        episodes = FileEpisodeStore(Path(scratch), overlay.episode_file)
        for index, hottest in enumerate(readings):
            clock[0] = start + timedelta(seconds=index * (overlay.min_hold_s + 60))
            handler = HandlerHostThermalGuard(
                InMemorySensors(
                    ModelThermalReading(
                        clock[0].strftime("%Y-%m-%dT%H:%M:%SZ"), hottest, ()
                    )
                ),
                holds,
                episodes,
                ledger,
                InMemoryLaneCutter(),
                lambda: clock[0],
            )
            result = handler.handle(
                ModelHostThermalGuardRequest(host=host, overlay=overlay)
            )
            sys.stdout.write(
                json.dumps(
                    {"simulated_hottest_c": hottest, **asdict(result)}, sort_keys=True
                )
                + "\n"
            )
            code = code or (EXIT_OK if result.ok else EXIT_UNDONE)
    return code


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.probe:
        sys.stdout.write(json.dumps({"status": PROBE_STATUS}, sort_keys=True) + "\n")
        return EXIT_OK
    try:
        overlay = load_overlay(os.environ)
    except (NodeOverlayError, ValidationError, ValueError) as exc:
        return _refuse(args, str(exc))
    host = (args.host or os.environ.get(overlay.host_env, "")).strip()
    if not host:
        return _refuse(args, f"{overlay.host_env} is not set and --host was not passed")
    try:
        row = overlay.host_row(host)
    except ValueError as exc:
        return _refuse(args, str(exc))
    if args.simulate_readings:
        try:
            readings = [float(x) for x in args.simulate_readings.split(",")]
        except ValueError:
            return _refuse(args, "--simulate-readings takes comma-separated numbers")
        return simulate(overlay, host, readings)
    command = shlex.split(os.environ.get(overlay.ledger_append.command_env, ""))
    if not command:
        return _refuse(
            args,
            f"{overlay.ledger_append.command_env} is not set; the guard has no ledger route",
        )
    if args.state_dir is None:
        return _refuse(
            args, "--state-dir is required: the unit's heartbeat lives there"
        )
    ledger_host = os.environ.get(overlay.ledger_append.host_env, "").strip() or host
    guard_dir = Path.home() / row.state_dir
    handler = HandlerHostThermalGuard(
        HostThermalSensors(Path(overlay.hwmon_root)),
        FileHoldStore(guard_dir, overlay.hold_file),
        FileEpisodeStore(guard_dir, overlay.episode_file),
        CommandLedgerAppender(
            command, row.lane, ledger_host, overlay.ledger_append.timeout_seconds
        ),
        HostLaneCutter(overlay.lane_scope_pattern, overlay.lane_tag_env),
    )
    result = handler.handle(
        ModelHostThermalGuardRequest(host=host, overlay=overlay, dry_run=args.dry_run)
    )
    line = {"status": "ok" if result.ok else "undone", **asdict(result)}
    code = EXIT_OK if result.ok else EXIT_UNDONE
    if not args.dry_run:
        write_tick_heartbeat(args.state_dir, code=code, line=line)
    sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
