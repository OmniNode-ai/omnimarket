# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The command line of the 17a cycle: ``--home`` for the registry home, ``--drill-error`` for the defined fault, ``--drill-offline SOURCE`` for a planted offline source,
``--check-overlays ROOT`` to load the three overlays from a root and say which is invalid (exit 1), ``--mvp-reads`` to
refresh the MVP launch block's scheduled read (a step of the floor publish) and ``--mvp-answer`` to answer one of the block's source
requests from stdin (the command the block's source port runs)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.cycle_interrupt import CycleInterrupted
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.floor_17a import ModelFloorSimpleTickRequest
from omnimarket.models.floor_17a.model_mvp_launch import ModelMvpLaunchOverlay
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    HandlerFloorSimpleTick,
    check_overlays,
    load_runtime,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.mvp_launch_reads import (
    CommandRunner,
    answer,
    refresh,
)


def _run_command(argv: Sequence[str], timeout: int) -> tuple[int, str, str]:
    try:
        done = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout} s"
    except OSError as exc:
        return 127, "", str(exc)
    return done.returncode, done.stdout, done.stderr


def _home(arg: str | None, env: Mapping[str, str]) -> Path | None:
    value = (arg or env.get(load_runtime().home_env, "")).strip()
    return Path(value) if value else None


def run_mvp_reads(
    home_arg: str | None,
    env: Mapping[str, str],
    run: CommandRunner = _run_command,
    clock: Callable[[], datetime] | None = None,
) -> int:
    """Refresh the MVP launch block's scheduled read. An overlay that cannot be read is exit 2: nothing is read from it."""
    rt = load_runtime().mvp_launch
    home = _home(home_arg, env)
    if home is None:
        sys.stderr.write("REFUSED mvp-reads: no registry home\n")
        return 2
    try:
        overlay = load_structured_overlay(
            rt.overlay.overlay_id,
            ModelMvpLaunchOverlay,
            env,
            file_env=rt.overlay.file_env,
        )
    except NodeOverlayError as exc:
        sys.stderr.write(f"REFUSED mvp-reads: {exc}\n")
        return 2
    record = refresh(
        overlay,
        home=home,
        onex_relpath=rt.onex_relpath,
        reads_relpath=rt.reads_relpath,
        read_timeout_seconds=rt.read_timeout_seconds,
        stage_deadline_seconds=rt.stage_deadline_seconds,
        refresh_seconds=rt.refresh_seconds,
        run=run,
        clock=clock or (lambda: datetime.now(UTC)),
    )
    failed = [r for r in record.reads if r.error is not None]
    sys.stdout.write(
        json.dumps({"workflows_read": len(record.reads), "unread": len(failed)}) + "\n"
    )
    return 0


def run_mvp_answer(
    home_arg: str | None,
    env: Mapping[str, str],
    request_text: str,
    now: datetime | None = None,
) -> int:
    """Answer one source request of the MVP launch block from the record; the answer carries its own reason when it cannot be had."""
    rt = load_runtime().mvp_launch
    home = _home(home_arg, env)
    if home is None:
        sys.stdout.write(json.dumps({"error": "no-registry-home"}) + "\n")
        return 0
    try:
        request = json.loads(request_text)
    except ValueError:
        request = None
    if not isinstance(request, dict):
        sys.stdout.write(json.dumps({"error": "request-not-an-object"}) + "\n")
        return 0
    sys.stdout.write(
        json.dumps(
            answer(
                request,
                reads_path=home / rt.reads_relpath,
                candidate_path=home / rt.candidate_relpath,
                reads_max_age_seconds=rt.reads_max_age_seconds,
                now=now or datetime.now(UTC),
            )
        )
        + "\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="floor_simple")
    ap.add_argument("--home", "--omni-home", dest="home", help="the registry home")
    ap.add_argument(
        "--state-dir",
        help="accepted for the module tick; this cycle keeps its own state",
    )
    ap.add_argument(
        "--drill-error",
        action="store_true",
        help="run the defined fault through the real reader and emit its typed failure",
    )
    ap.add_argument(
        "--drill-offline",
        metavar="SOURCE",
        help="plant one lab source as offline and mark every alarm it raises as a test; use it with a scratch --home",
    )
    ap.add_argument(
        "--defer-canary",
        action="store_true",
        help="do not publish the canary status in this cycle; the publish node runs --publish-canary after the page is served",
    )
    ap.add_argument(
        "--publish-canary",
        action="store_true",
        help="publish only the canary status, from the page as it is served now",
    )
    ap.add_argument(
        "--check-overlays", metavar="ROOT", help="load the overlays under ROOT and exit"
    )
    ap.add_argument(
        "--mvp-reads",
        action="store_true",
        help="refresh the MVP launch block's scheduled read of its producers' runs and exit",
    )
    ap.add_argument(
        "--mvp-answer",
        action="store_true",
        help="answer one MVP launch source request (JSON on stdin) from the record and exit",
    )
    args = ap.parse_args(argv)
    if args.mvp_reads:
        return run_mvp_reads(args.home, os.environ)
    if args.mvp_answer:
        return run_mvp_answer(args.home, os.environ, sys.stdin.read())
    if args.check_overlays:
        try:
            for line in check_overlays(Path(args.check_overlays)):
                sys.stdout.write(line + "\n")
        except (NodeOverlayError, ValidationError, OSError) as exc:
            sys.stderr.write(f"INVALID overlay: {type(exc).__name__}: {exc}\n")
            return 1
        return 0
    try:
        result = HandlerFloorSimpleTick().run(
            ModelFloorSimpleTickRequest(
                home=args.home,
                drill_error=args.drill_error,
                drill_offline=args.drill_offline,
                defer_canary=args.defer_canary,
                canary_only=args.publish_canary,
            )
        )
    except NodeOverlayError as exc:
        sys.stderr.write(f"REFUSED floor-simple: {exc}\n")
        return 2
    except CycleInterrupted as exc:
        sys.stderr.write(f"INTERRUPTED floor-simple: {exc.code}: {exc.detail}\n")
        return 3
    sys.stdout.write(result.model_dump_json(indent=1) + "\n")
    if result.failure_code == "drill_completed":
        return 0
    return 0 if result.ok else 2
