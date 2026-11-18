# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick of the session stall alarm, the unit the host installer renders for the operator Mac (OMN-25405).

Reads the newest session transcript the overlay names, judges the queue rows, and delivers a stall through the lab alarm's
delivery. The unit passes ``--omni-home`` and ``--state-dir``. Prints one line per tick, the reading the host shows
after the unit has run. Exit 0 when the session reads healthy or a stall was read and delivered, 3 when the transcript
or an overlay could not be read, 2 when delivery failed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.lab_alarm_delivery import EnumDeliveryOutcome
from omnimarket.models.session_stall import (
    EnumSessionStallOutcome,
    ModelSessionStallTickRequest,
    ModelSessionStallTickResult,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import (
    load_overlay as load_delivery_overlay,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.runtime_lab_alarm_deliver import (
    build_handler as build_delivery_handler,
)
from omnimarket.nodes.node_session_stall_alarm_compute.handlers.handler_session_stall_judge import (
    HandlerSessionStallJudge,
)
from omnimarket.nodes.node_session_stall_alarm_effect.adapters.transcript_reader import (
    FileTranscriptReader,
)
from omnimarket.nodes.node_session_stall_alarm_effect.handlers.handler_session_stall_alarm import (
    HandlerSessionStallAlarm,
)
from omnimarket.nodes.node_session_stall_alarm_effect.overlay import (
    NODE_NAME,
    load_overlay,
)

EXIT_DELIVERY_FAILED = 2
EXIT_UNREADABLE = 3
CONTRACT = Path(__file__).resolve().parent / "contract.yaml"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def exit_for(result: ModelSessionStallTickResult) -> int:
    if result.outcome is EnumSessionStallOutcome.INDETERMINATE:
        return EXIT_UNREADABLE
    if result.delivery.outcome is EnumDeliveryOutcome.FAILED:
        return EXIT_DELIVERY_FAILED
    return 0


def summary_line(result: ModelSessionStallTickResult, at: datetime) -> str:
    verdict = result.verdict
    cells = [
        f"session-stall tick {at.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"outcome={result.outcome.value}",
        f"delivery={result.delivery.outcome.value}",
    ]
    if verdict is not None:
        cells.append(f"queued={verdict.queued}")
        cells.append(
            "reasons=" + (",".join(r.value for r in verdict.reasons) or "none")
        )
    if result.failure:
        cells.append(f"failure={result.failure}")
    return " ".join(cells)


def build_handler(
    clock: Callable[[], datetime] = _utc_now,
) -> HandlerSessionStallAlarm:
    overlay = load_overlay()
    return HandlerSessionStallAlarm(
        overlay=overlay,
        reader=FileTranscriptReader(
            glob=overlay.transcript_glob,
            tail_bytes=overlay.tail_bytes,
            window_seconds=overlay.window_seconds,
            task_markers=overlay.task_markers,
        ),
        judge=HandlerSessionStallJudge(),
        delivery=build_delivery_handler(load_delivery_overlay(), clock=clock),
        clock=clock,
    )


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    held = path.with_suffix(path.suffix + ".tmp")
    held.write_text(text, encoding="utf-8")
    held.replace(path)


def tick_deadline_s() -> float:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    return float(contract["descriptor"]["timeout_ms"]) / 1000


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"{NODE_NAME}.tick", description=__doc__)
    parser.add_argument("--omni-home", dest="registry_root", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument(
        "--probe",
        action="store_true",
        help="judge and print the reading, deliver nothing and keep nothing",
    )
    args = parser.parse_args(argv)
    try:
        handler = build_handler()
    except NodeOverlayError as exc:
        sys.stderr.write(f"{NODE_NAME}: {exc}\n")
        return EXIT_UNREADABLE
    at = _utc_now()
    result = handler.handle(
        ModelSessionStallTickRequest(
            correlation_id=f"session-stall-{at.strftime('%Y%m%dT%H%M%SZ')}",
            write=not args.probe,
        )
    )
    line = summary_line(result, at)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
    code = exit_for(result)
    if not args.probe:
        _write_atomically(
            args.state_dir / "last-run.json",
            json.dumps({"ran_at": at.isoformat(), "exit_code": code, "line": line}),
        )
    return code


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["build_handler", "exit_for", "main", "summary_line"]
