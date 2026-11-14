# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Mac lab-fill unit's entry: one fill pass with no model in the loop (OMN-24419).

    python -m omnimarket.nodes.node_lab_fill_effect.runtime_lab_fill_tick --omni-home <root> --state-dir <dir>

One pass reads the unit's overlay (node_lab_fill_effect), the lane host pool snapshot the pool writer keeps
(the free slots per host), and the approved-work list; runs the select step on the bus ledger reader's rows
(select_entry) and, only when that read was ok, the whole tick (tick_entry) with the select step's holds.
Every input that is stale, unreadable or not moved ends the pass with a typed REFUSED line and a non-zero
exit before any lane is launched; no input is ever replaced by an older file.

Exit 0: the tick ran (what it placed is read from its own line). Exit 69: an input was unavailable, nothing
was launched. Exit 78: an overlay could not be loaded. Exit 2 or 3: the tick or select entry's own code.
"""

from __future__ import annotations

import argparse
import dataclasses
import io
import json
import os
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from omnibase_spi.contracts.services import ModelLaneHostPoolRequest

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.lab_fill.model_lab_fill_selection_overlay import (
    ModelLabFillSelectionOverlay,
)
from omnimarket.models.model_lane_host_pool_unreadable import (
    LaneHostPoolUnreadableError,
)
from omnimarket.nodes.node_friction_rollup_tick_effect.handlers.friction_rollup_tick_io import (
    write_atomic,
)
from omnimarket.nodes.node_lab_fill_effect.handlers.handler_lab_fill_select import (
    HandlerLabFillSelect,
)
from omnimarket.nodes.node_lab_fill_effect.handlers.handler_lab_fill_tick import (
    HandlerLabFillTick,
)
from omnimarket.nodes.node_lab_fill_effect.models.model_lab_fill_select import (
    ModelLabFillSelectRequest,
    ModelLabFillSelectResult,
)
from omnimarket.nodes.node_lab_fill_effect.models.model_lab_fill_tick import (
    ModelLabFillTickHost,
)
from omnimarket.nodes.node_lab_fill_effect.models.model_lab_fill_tick_overlay import (
    OVERLAY_FILE_ENV,
    OVERLAY_ID,
    ModelLabFillTickOverlay,
)
from omnimarket.nodes.node_lab_fill_effect.select_entry import main as run_select_entry
from omnimarket.nodes.node_lab_fill_effect.tick_entry import main as run_tick_entry
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.adapter_snapshot_file import (
    StampedSnapshotFileLaneHostPool,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.pool_snapshot_sources import (
    resolve_path,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.snapshot_errors import (
    PoolSnapshotSourceError,
)

PROBE_FLAG = "--probe"
PROBE_STATUS = "probe_ok"
EVIDENCE_FILE = "last-run.json"
_EXIT_INPUT_UNAVAILABLE = 69
_EXIT_OVERLAY = 78
_EXIT_SELECT_FAILED = 3
_STEP = "run_lab_fill_unit"


class _RefusedError(Exception):
    """An input of the pass was unavailable: the typed reason, the detail and the exit code."""

    def __init__(self, reason: str, detail: str, code: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail
        self.code = code


def _hosts_from_snapshot(
    overlay: ModelLabFillTickOverlay,
    clock: Callable[[], datetime],
) -> tuple[ModelLabFillTickHost, ...]:
    """Each host the pool admits lanes on, with its free lane slots (the snapshot's lane_slots, as SnapshotFloorPool sums them)."""
    pool = StampedSnapshotFileLaneHostPool(
        resolve_path(overlay.pool_snapshot_path),
        overlay.pool_record_suffix,
        overlay.pool_snapshot_max_age_seconds,
        clock,
    )
    try:
        snapshot = pool.read_pool(ModelLaneHostPoolRequest(engine=overlay.pool_engine))
    except (
        LaneHostPoolUnreadableError,
        PoolSnapshotSourceError,
        OSError,
        ValueError,
    ) as exc:
        raise _RefusedError("pool-snapshot", str(exc), _EXIT_INPUT_UNAVAILABLE) from exc
    return tuple(
        sorted(
            (
                ModelLabFillTickHost(host=reading.name, free=reading.lane_slots)
                for reading in snapshot.readings
                if reading.lane_admission_refusal is None and reading.lane_slots > 0
            ),
            key=lambda host: host.host,
        )
    )


def _candidates_from_approved_work(path: Path) -> list[dict[str, object]]:
    """The approved-work rows as the select step's candidates (key, kind, ticket, repo)."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _RefusedError(
            "approved-work", type(exc).__name__, _EXIT_INPUT_UNAVAILABLE
        ) from exc
    rows = document.get("rows") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise _RefusedError("approved-work", "no rows list", _EXIT_INPUT_UNAVAILABLE)
    candidates: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key, kind, ticket = row.get("id"), row.get("kind"), row.get("ticket")
        if not (
            isinstance(key, str) and isinstance(kind, str) and isinstance(ticket, str)
        ):
            continue
        repo = row.get("repo")
        candidates.append(
            {
                "key": key,
                "kind": kind,
                "ticket": ticket,
                "repo": repo if isinstance(repo, str) else "",
            }
        )
    return candidates


def _select_holds(
    overlay: ModelLabFillTickOverlay,
    candidates: list[dict[str, object]],
    environ: Mapping[str, str],
    now: datetime,
    selector: HandlerLabFillSelect | None,
    stderr: TextIO,
) -> dict[str, str]:
    """Run the select step through its entry; a not-ok ledger read refuses the pass."""
    try:
        deployment = ModelLabFillSelectionOverlay.load_deployment(environ)
    except (NodeOverlayError, ValueError) as exc:
        raise _RefusedError("overlay", type(exc).__name__, _EXIT_OVERLAY) from exc
    request = ModelLabFillSelectRequest(
        selection={
            "now": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "candidates": candidates,
            "deployment": dataclasses.asdict(deployment),
            "landing_lane": overlay.landing_lane,
            "attempt_limit": overlay.selection_attempt_limit,
            "attempt_window_hours": overlay.selection_attempt_window_hours,
            "cooldown_hours": overlay.selection_cooldown_hours,
            "repeat_limit": overlay.selection_repeat_limit,
            "scope_repos": list(overlay.selection_scope_repos),
        },
        ledger_reader=overlay.selection_ledger_reader,
        ledger_window_hours=overlay.selection_ledger_window_hours,
    )
    out, err = io.StringIO(), io.StringIO()
    code = run_select_entry(
        stdin=io.StringIO(request.model_dump_json()),
        stdout=out,
        stderr=err,
        selector=selector,
        environ=environ,
    )
    stderr.write(err.getvalue())
    if code == 0:
        return ModelLabFillSelectResult.model_validate_json(out.getvalue()).holds()
    if code == _EXIT_INPUT_UNAVAILABLE:
        result = ModelLabFillSelectResult.model_validate_json(out.getvalue())
        raise _RefusedError(
            f"ledger-{result.ledger_status}",
            result.ledger_reason,
            _EXIT_INPUT_UNAVAILABLE,
        )
    raise _RefusedError(f"select-exit-{code}", "", _EXIT_SELECT_FAILED)


def run_once(
    overlay: ModelLabFillTickOverlay,
    *,
    environ: Mapping[str, str],
    clock: Callable[[], datetime],
    selector: HandlerLabFillSelect | None = None,
    ticker: HandlerLabFillTick | None = None,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """One pass: the exit code. The tick runs only when every input was read."""
    now = clock()
    try:
        hosts = _hosts_from_snapshot(overlay, clock)
        candidates = _candidates_from_approved_work(
            resolve_path(overlay.approved_work_path)
        )
        holds = _select_holds(overlay, candidates, environ, now, selector, stderr)
    except _RefusedError as refusal:
        stderr.write(
            f"lab-fill unit: step={_STEP} REFUSED reason={refusal.reason} detail={refusal.detail}\n"
        )
        return refusal.code
    request = overlay.tick_request(
        run_key=now.strftime("%Y-%m-%dT%H%MZ"),
        fire_id=now.strftime("%Y%m%dT%H%M%SZ") + "-lab-fill",
        hosts=hosts,
        selection_holds=holds,
        ledger_path=str(resolve_path(overlay.ledger_path)),
        approved_work_path=str(resolve_path(overlay.approved_work_path)),
        brief_dir=str(resolve_path(overlay.brief_dir)),
        state_path=str(resolve_path(overlay.state_path)) if overlay.state_path else "",
    )
    stderr.write(
        f"lab-fill unit: step={_STEP} hosts={len(hosts)} held={len(holds)}"
        f" free={sum(host.free for host in hosts)}\n"
    )
    return run_tick_entry(
        stdin=io.StringIO(request.model_dump_json()),
        stdout=stdout,
        stderr=stderr,
        ticker=ticker,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omnimarket.nodes.node_lab_fill_effect.runtime_lab_fill_tick",
        description="One lab-fill pass: read the pool snapshot, select on the bus ledger rows, run the tick",
    )
    parser.add_argument(
        PROBE_FLAG,
        action="store_true",
        help="Start check: exit 0 and print one status line without loading an overlay, reading the bus or writing a file",
    )
    parser.add_argument(
        "--omni-home",
        help="The registry root the installer's unit passes (accepted; the overlay names every path)",
    )
    parser.add_argument(
        "--state-dir",
        help="Where last-run.json, the unit's heartbeat, is written after a pass",
    )
    return parser


def _write_heartbeat(state_dir: str | None, code: int, now: datetime) -> None:
    """Leave last-run.json beside the unit's other state so the install's heartbeat check reads the last pass."""
    if not state_dir:
        return
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    evidence = {
        "status": "ran" if code == 0 else "refused-or-failed",
        "exit_code": code,
        "written_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    write_atomic(Path(state_dir) / EVIDENCE_FILE, json.dumps(evidence, indent=2) + "\n")


def main(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    selector: HandlerLabFillSelect | None = None,
    ticker: HandlerLabFillTick | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """--probe exits 0 after one status line, before loading the overlay or running the pass."""
    args = _parser().parse_args(argv)
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    if args.probe:
        out.write(json.dumps({"status": PROBE_STATUS}, sort_keys=True) + "\n")
        return 0
    env = os.environ if environ is None else environ
    try:
        overlay = load_structured_overlay(
            OVERLAY_ID, ModelLabFillTickOverlay, env, file_env=OVERLAY_FILE_ENV
        )
    except (NodeOverlayError, ValueError) as exc:
        err.write(
            f"lab-fill unit: step={_STEP} REFUSED reason=overlay detail={type(exc).__name__}\n"
        )
        code = _EXIT_OVERLAY
    else:
        code = run_once(
            overlay,
            environ=env,
            clock=clock,
            selector=selector,
            ticker=ticker,
            stdout=out,
            stderr=err,
        )
    _write_heartbeat(args.state_dir, code, clock())
    return code


__all__: list[str] = ["main", "run_once"]

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
