# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The timer's entry point of the stop drill: one run, then the timer's evidence.

``python -m omnimarket.nodes.node_stop_drill_effect run`` is the command the installed unit runs. Every setting comes
from the drill's overlay (``ONEX_SKILL_OVERLAY_ROOTS``). Exit codes: 0 the drill read WORKING, 1 it read NOT WORKING or
UNKNOWN, 65 the overlay refused, 74 the ledger did not take the run's rows. The timer's own evidence is
``DIR/last-run.json`` (run id, verdict, exit code), written after every run, so a timer that stopped can be told from a
drill that reads NOT WORKING.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.emission_sink import JsonlEmissionSink
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    build_appender,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelLedgerAppendRoute
from omnimarket.nodes.node_stop_drill_effect.handlers.handler_stop_drill import (
    HandlerStopDrill,
    StopDrillLedgerAppendError,
    host_from_environment,
)
from omnimarket.nodes.node_stop_drill_effect.models import ModelStopDrillRequest
from omnimarket.nodes.node_stop_drill_effect.overlay import load_overlay

EXIT_WORKING = 0
EXIT_NOT_WORKING = 1
EXIT_OVERLAY_REFUSED = 65
EXIT_LEDGER_UNCONFIRMED = 74


def run_once(
    run_id: str | None,
    *,
    trigger: str,
    emit: Callable[[str], None],
    emit_error: Callable[[str], None],
    environ: Mapping[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run the drill once. Returns the exit code, the run id and the verdict ("" when none was read)."""
    env = os.environ if environ is None else environ
    try:
        overlay = load_overlay(env)
        ledger = overlay.emission.ledger
        if ledger is None:
            raise NodeOverlayError("the overlay names no ledger rows")
        home = env.get(overlay.emission.home_env, "").strip()
        if not home:
            raise NodeOverlayError(
                f"{overlay.emission.home_env} is not set; the drill's emission journal is relative to it"
            )
        appender = build_appender(
            ModelLedgerAppendRoute(
                command_env=ledger.command_env,
                host_env=ledger.host_env,
                timeout_seconds=ledger.timeout_seconds,
            ),
            ledger.lane,
            env,
        )
        host = host_from_environment(overlay, env)
    except (NodeOverlayError, ValidationError, ValueError, RuntimeError) as exc:
        emit_error(f"overlay refused: {exc}")
        return EXIT_OVERLAY_REFUSED, run_id or "", ""
    now = datetime.now(UTC)
    request = ModelStopDrillRequest(
        run_id=run_id or f"{trigger}-{now:%Y%m%dT%H%M%SZ}",
        requested_at=now,
        trigger=trigger,
    )
    handler = HandlerStopDrill(
        overlay,
        appender=appender,
        host=host,
        sink=JsonlEmissionSink(Path(home) / overlay.emission.journal_relpath),
    )
    try:
        result = handler.handle(request)
    except StopDrillLedgerAppendError as exc:
        emit_error(f"ledger: {exc}")
        return EXIT_LEDGER_UNCONFIRMED, request.run_id, ""
    emit(result.model_dump_json(indent=2))
    code = EXIT_WORKING if result.verdict == "WORKING" else EXIT_NOT_WORKING
    return code, request.run_id, result.verdict


def write_evidence(
    state_dir: Path, run_id: str, verdict: str, code: int, finished: datetime
) -> None:
    """The timer's evidence, written after every run and replaced whole."""
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / "last-run.json"
    temporary = state_dir / f".last-run.{os.getpid()}.json"
    temporary.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "verdict": verdict or "none",
                "exit_code": code,
                "finished_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    temporary.replace(target)
