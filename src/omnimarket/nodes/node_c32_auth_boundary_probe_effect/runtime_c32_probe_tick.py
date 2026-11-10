# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One timer tick of the C32 auth-boundary probe, the unit the host installer renders (OMN-23213).

The unit passes ``--omni-home`` and ``--state-dir``. A tick runs the probe once with the overlay's default profile,
puts the run record on the bus (the contract's result topic), appends it to the run history under the state directory
and writes ``<state-dir>/last-run.json``.

Exit codes: 0 the probe PASSed and its record was emitted; 1 the run concluded failure (FAIL, PRECONDITION-ABSENT or
ERROR): the record is still emitted and the unit shows failed; 2 the overlay cannot be loaded; 3 the record was
not accepted by the bus. The evidence file is written for every code.
"""

from __future__ import annotations

import argparse
import faulthandler
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.events.emit_effect_topic_publisher import EmitEffectTopicPublisher
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_c32_auth_boundary_probe_effect.handlers.handler_c32_auth_boundary_probe import (
    NODE,
    HandlerC32AuthBoundaryProbe,
    build_prober,
    load_overlay,
)
from omnimarket.nodes.node_c32_auth_boundary_probe_effect.models.model_c32_probe import (
    ModelC32ProbeRequest,
    ModelC32ProbeResult,
)
from omnimarket.nodes.node_c32_auth_boundary_probe_effect.protocols.protocol_c32_prober import (
    ProtocolC32ResultPublisher,
)

EVIDENCE_FILE = "last-run.json"
EXIT_FAILED = 1
EXIT_OVERLAY = 2
EXIT_NOT_PUBLISHED = 3


def tick_deadline_s() -> float:
    contract = yaml.safe_load(
        (Path(__file__).parent / "contract.yaml").read_text(encoding="utf-8")
    )
    return float(contract["descriptor"]["timeout_ms"]) / 1000


def evidence_for(result: ModelC32ProbeResult) -> dict[str, Any]:
    return {
        "node": NODE,
        "ran_at": result.created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_id": result.run_id,
        "event": result.event,
        "profile": result.profile,
        "status": result.status,
        "conclusion": result.conclusion,
        "verdict": result.verdict.value,
        "clauses_ok": sum(1 for c in result.clauses if c.ok),
        "clauses": len(result.clauses),
        "failed_clauses": [c.name for c in result.clauses if not c.ok],
        "absent": [p.name for p in result.preconditions if not p.present],
        "emitted": result.emitted,
        "error": result.error,
    }


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    held = path.with_suffix(path.suffix + ".tmp")
    held.write_text(text, encoding="utf-8")
    held.replace(path)


def append_history(path: Path, result: ModelC32ProbeResult, keep: int) -> None:
    """One JSON line per run (the run in ``ModelMvpRun`` shape plus the verdict), newest last, trimmed to ``keep``."""
    line = json.dumps(
        {**result.mvp_run(), "verdict": result.verdict.value}, sort_keys=True
    )
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    _write_atomically(path, "\n".join([*lines, line][-keep:]) + "\n")


def main(
    argv: list[str] | None = None,
    *,
    handler: HandlerC32AuthBoundaryProbe | None = None,
    publisher: ProtocolC32ResultPublisher | None = None,
    now: datetime | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog=f"{NODE}.tick", description=__doc__)
    parser.add_argument("--omni-home", dest="registry_root", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--trigger", choices=("schedule", "manual"), default="schedule")
    args = parser.parse_args(argv)
    faulthandler.dump_traceback_later(tick_deadline_s(), exit=True)
    stamp = now or datetime.now(UTC)
    evidence_path = args.state_dir / EVIDENCE_FILE
    try:
        overlay = load_overlay()
        profile = overlay.default_profile
        run_handler = handler or HandlerC32AuthBoundaryProbe(
            prober=build_prober(overlay, profile),
            publisher=publisher or EmitEffectTopicPublisher(),
            topic=overlay.result_topic,
            profile=profile,
        )
    except (NodeOverlayError, ValueError, OSError) as exc:
        faulthandler.cancel_dump_traceback_later()
        reading = {
            "node": NODE,
            "ran_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "overlay_unreadable",
            "error": f"{type(exc).__name__}: {exc}"[-2000:],
        }
        _write_atomically(evidence_path, json.dumps(reading, sort_keys=True) + "\n")
        sys.stderr.write(f"{NODE}: {reading['error']}\n")
        return EXIT_OVERLAY
    try:
        result = run_handler.handle(
            ModelC32ProbeRequest(
                correlation_id=f"c32-probe-{uuid.uuid4().hex[:12]}",
                trigger=args.trigger,
            )
        )
    finally:
        faulthandler.cancel_dump_traceback_later()
    append_history(args.state_dir / overlay.history_file, result, overlay.history_keep)
    reading = evidence_for(result)
    _write_atomically(evidence_path, json.dumps(reading, sort_keys=True) + "\n")
    sys.stdout.write(json.dumps(reading, sort_keys=True) + "\n")
    if not result.emitted:
        return EXIT_NOT_PUBLISHED
    return 0 if result.conclusion == "success" else EXIT_FAILED


__all__ = ["EVIDENCE_FILE", "append_history", "evidence_for", "main"]

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
