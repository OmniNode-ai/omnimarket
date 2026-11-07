# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host timer's entry point of the central test harness: one run of every case, then the timer's evidence.

``python -m omnimarket.nodes.node_lab_test_harness_effect.runtime_lab_test_harness --omni-home ROOT --state-dir DIR``
is the command the installed ``lab-test-harness`` unit runs (node_host_installer_registry_units). It runs the same
handler as the ``run`` verb and the schedule node's request. Every setting comes from the harness overlay
(``ONEX_SKILL_OVERLAY_ROOTS``); the two flags are the timer's registry home and evidence directory.

The harness writes its reading to the overlay's ``current_result_path``, where the floor reads it. The timer's own
evidence is ``DIR/last-run.json``: the run id, the reading state and the exit code, written after every run, so a
timer that stopped can be told from a harness that reads NOT WORKING. The exit code is 0 when the reading is
WORKING, 1 when it is not, 69 when the bus is unavailable and 65 when the overlay refuses.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.lab_test_harness import (
    EnumLabTestHarnessReadingState,
    ModelLabTestHarnessOverlay,
    ModelLabTestHarnessRequested,
)
from omnimarket.nodes.node_lab_test_harness_effect.handlers import (
    HandlerLabTestHarnessEffect,
    open_lab_work_runner,
)
from omnimarket.nodes.node_lab_test_harness_effect.handlers.handler_lab_test_harness_effect import (
    sink_from_overlay,
)

EXIT_WORKING = 0
EXIT_NOT_WORKING = 1
EXIT_OVERLAY_REFUSED = 65
EXIT_BUS_UNAVAILABLE = 69
NODE_NAME = "node_lab_test_harness_effect"


def run_once(
    workspace_root: Path | None,
    run_id: str | None,
    *,
    trigger: str,
    emit: Callable[[str], None],
    emit_error: Callable[[str], None],
) -> tuple[int, str, str]:
    """Run every case once. Returns the exit code, the run id and the reading state ("" when none was read)."""
    try:
        overlay = load_structured_overlay(NODE_NAME, ModelLabTestHarnessOverlay)
        sink = sink_from_overlay(overlay)
    except (NodeOverlayError, ValidationError) as exc:
        emit_error(f"overlay refused: {exc}")
        return EXIT_OVERLAY_REFUSED, run_id or "", ""
    now = datetime.now(UTC)
    handler = HandlerLabTestHarnessEffect(
        overlay=overlay,
        runner_opener=lambda ov: open_lab_work_runner(
            ov, workspace_root=workspace_root
        ),
        sink=sink,
    )
    request = ModelLabTestHarnessRequested(
        run_id=run_id or f"{trigger}-{now:%Y%m%dT%H%M%SZ}",
        requested_at=now,
        trigger=trigger,
    )
    try:
        reading = asyncio.run(handler.handle(request))
    except LabRunBusError as exc:
        emit_error(f"bus: {exc}")
        return EXIT_BUS_UNAVAILABLE, request.run_id, ""
    emit(reading.model_dump_json(indent=2))
    code = (
        EXIT_WORKING
        if reading.state is EnumLabTestHarnessReadingState.WORKING
        else EXIT_NOT_WORKING
    )
    return code, request.run_id, reading.state.value


def write_evidence(
    state_dir: Path, run_id: str, state: str, code: int, finished: datetime
) -> None:
    """The timer's evidence, written after every run and replaced whole."""
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / "last-run.json"
    temporary = state_dir / f".last-run.{os.getpid()}.json"
    temporary.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "reading_state": state or "none",
                "exit_code": code,
                "finished_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    temporary.replace(target)


def _stdout(text: str) -> None:
    sys.stdout.write(text + "\n")


def _stderr(text: str) -> None:
    sys.stderr.write(text + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False, description=__doc__)
    parser.add_argument("--omni-home", dest="registry_root", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    code, run_id, state = run_once(
        args.registry_root,
        None,
        trigger="schedule",
        emit=_stdout,
        emit_error=_stderr,
    )
    write_evidence(args.state_dir, run_id, state, code, datetime.now(UTC))
    return code


if __name__ == "__main__":
    sys.exit(main())
