# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A remote lane that has not been placed never reads 'placing' forever (OMN-17427).

The throughput tick of 2026-10-18T01:45Z showed about 15 lanes reading 'placing' on the launching host for hours while
one lab host had 9 free Claude slots. 26 receipts there were `status: placing`, never final, their runner pid dead
(one lane since 21:09Z, pinned one lab host): the runner stopped during placement (a kill, or a
SIGTERM, which exits through SystemExit before any receipt is final), so it never ended no-host, the lane queue never held
it, and the tick's no-host re-placement, which reads only final no-host receipts, never saw it. Nothing would ever start
those lanes.

Two operations close it. The runner (onex_remote_lane.py) ends its own receipt with the reason when it is stopped before
the lane is placed (``until_placed``, ``end_unplaced``). The throughput tick ends every receipt a dead runner left placing
and, while the lane queue's term lasts, puts the lane back in the queue, unpinned, so the next free slot starts it
(``requeue_lost_placements``). ``placement_wait`` and ``without_queued_no_host`` are the two readers of that status.

A host the runner's placement refused is not free headroom (``placement_refusals``, OMN-17427). 2026-10-19 00:40-01:25Z the
tick printed 'MISSING lab-headroom:one lab host free Claude slots 4' and 'one lab host free 1' while the runner's own read refused those hosts
('ADMISSION-REFUSED(CPU-IDLE-UNDER-BAR: 1.9% idle < 25%; RESERVE-OWN-LOAD ...)'), and 27 lanes ended no-host in the hour: one
probe of a Mac's CPU idle or of a host's own load reads free between two refusals. The runner's newest read of each host inside
the tick's own interval, kept in its receipt, is judged by the same admission the runner applies (``Reading.from_description``,
``lane_admission_refusal``); a newer read that admits the host, or none in the window, leaves the tick's own probe to decide.

The runtime is the calling script's globals: the runner's exit codes, ``write_receipt`` and ``_now``; the tick's
``_load_placement``, ``lc``, ``_read_json``, ``_relaunch_argv`` and ``_write_json_atomic``.
"""

from __future__ import annotations

import copy
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from ..models.model_claude_account_limit import (
    ModelClaudeAccountLimitRequest,
    ModelClaudeAccountLimitResult,
)
from ..models.model_codex_lane_limit import (
    ModelCodexLaneLimitRequest,
    ModelCodexLaneLimitResult,
)
from ..models.model_lane_engine_failover import (
    ModelLaneEngineFailoverRequest,
    ModelLaneEngineFailoverResult,
)
from .handler_claude_account_limit import HandlerClaudeAccountLimit
from .handler_codex_lane_limit import CODEX_LIMITED_PREFIX, HandlerCodexLaneLimit, _read
from .handler_lane_engine_failover import HandlerLaneEngineFailover

LOST_STATUS = "placement-lost"
PLACEMENT_REFUSAL_WINDOW_S = 600.0  # the throughput tick's own interval
LOST_GRACE_S = 180  # a dead runner's receipt is judged after this long, past the detached parent's last re-read
LOST_EXIT = 70  # the runner's EXIT_REMOTE_FAILED
SIGTERM_EXIT = 143
# The placement module's pid probe, read from the runtime port.
PID_ALIVE_ATTR = "_pid_alive"


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


class HandlerLanePlacementRecovery:
    """Receipt and lane queue operations for a lane that has not been placed."""

    def __init__(self, runtime: Mapping[str, Any]) -> None:
        self.runtime = runtime
        self.account_handler = HandlerClaudeAccountLimit(runtime)
        self.engine_handler = HandlerLaneEngineFailover(runtime)

    def handle(
        self,
        request: ModelClaudeAccountLimitRequest
        | ModelLaneEngineFailoverRequest
        | ModelCodexLaneLimitRequest,
    ) -> (
        ModelClaudeAccountLimitResult
        | ModelLaneEngineFailoverResult
        | ModelCodexLaneLimitResult
    ):
        if isinstance(request, ModelCodexLaneLimitRequest):
            return HandlerCodexLaneLimit().handle(request)
        if isinstance(request, ModelLaneEngineFailoverRequest):
            return self.engine_handler.handle(request)
        return self.account_handler.handle(request)

    def execute(self, args: Any, **kwargs: Any) -> int:
        request = ModelLaneEngineFailoverRequest(args, self.execute_observed, kwargs)
        return self.engine_handler.handle(request).exit_code

    def execute_observed(self, args: Any, **kwargs: Any) -> int:
        """Write the Codex marker before an attempt can be retried or returned."""
        current = copy.copy(args)
        current.run_id = (
            args.run_id
            or f"rlane-{args.lane[:40]}-{self.runtime['secrets'].token_hex(3)}"
        )
        path = (
            Path(args.receipt)
            if args.receipt
            else self.runtime["state_dir"]() / args.lane / f"{current.run_id}.json"
        )
        current.receipt = str(path)
        code = self.runtime["execute_once"](current, **kwargs)
        if current.model == "codex":
            HandlerCodexLaneLimit().handle(
                ModelCodexLaneLimitRequest(
                    str(path),
                    str(self.runtime["landing_placement"].placement_dir()),
                    completed_attempt=True,
                )
            )
        return cast("int", code)

    def codex_placement_facts(self, request: dict[str, Any]) -> dict[str, Any]:
        """Add engine-specific marks to the pure placement request under the caller's lock."""
        lp = self.runtime["landing_placement"]
        directory = lp.placement_dir()
        wire = copy.deepcopy(request)
        wire["codex_limited_hosts"] = sorted(lp.codex_limited_hosts(directory))
        return wire

    def codex_tick_facts(self, request: dict[str, Any]) -> dict[str, Any]:
        """Count today's refused runs once and apply markers to the tick's live read."""
        from .handler_lane_engine_failover import tick_request

        wire = tick_request(request)
        facts = wire.get("lab_headroom")
        if not isinstance(facts, dict):
            return wire
        lp = self.runtime["_load_placement"]()
        if lp is None:
            return wire
        directory = lp.placement_dir()
        now = datetime.fromisoformat(
            str(wire["now"]).replace("Z", "+00:00")
        ).astimezone(UTC)
        counts = {str(host["name"]): 0 for host in facts.get("hosts", [])}
        seen = set()
        paths = [
            str(receipt["path"])
            for receipt in facts.get("receipts", [])
            if receipt.get("path")
        ]
        visited = set()
        for path in paths:
            if path in visited:
                continue
            visited.add(path)
            data = _read(Path(path))
            # A final root receipt can name a different engine after failover.
            # The attempt receipts retain the Codex event and host identity.
            for attempt in (data.get("failover") or {}).get("attempts", []):
                if attempt.get("receipt"):
                    paths.append(str(attempt["receipt"]))
            if not str(data.get("ended_at", "")).startswith(now.date().isoformat()):
                continue
            result = HandlerCodexLaneLimit().handle(
                ModelCodexLaneLimitRequest(str(path), str(directory), now)
            )
            key = (data.get("host"), data.get("run_id") or str(path))
            if result.observed_429 and key not in seen:
                seen.add(key)
                name = str(data["host"])
                counts[name] = counts.get(name, 0) + 1
        facts["codex_429_counts"] = counts
        for host in facts.get("hosts", []):
            marker = directory / f"{CODEX_LIMITED_PREFIX}{host['name']}"
            if marker.exists():
                body = _read(marker)
                host["codex_limited_mark"] = (
                    {"state": "present", "until": body["until"]}
                    if body.get("until")
                    else {"state": "unreadable"}
                )
        return wire

    # ---- the runner ----------------------------------------------------------------------------------------------------

    def placement_wait(
        self,
        receipt: Path,
        max_s: float,
        *,
        lane: str,
        alive: Callable[[], bool],
        poll_s: float = 1.0,
        echo: Callable[[str], None] = print,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> int:
        """Report placement before the detached parent exits (OMN-20840)."""
        rt = self.runtime
        end = clock() + max_s
        dead = False
        while True:
            try:
                data = json.loads(receipt.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            status, host = data.get("status") or "placing", data.get("host") or "none"
            if data.get("final"):
                if status == "no-host":
                    reason = " ".join(str(data.get("reason") or "unrecorded").split())
                    echo(
                        f"PLACEMENT lane={lane} status=no-host reason={reason} receipt={receipt}"
                    )
                    return cast("int", rt["EXIT_NO_HOST"])
                if status == "done":
                    echo(
                        f"PLACEMENT lane={lane} status=done host={host} receipt={receipt}"
                    )
                    return cast("int", rt["EXIT_OK"])
                failure = data.get("failure")
                reason = (
                    data.get("reason")
                    or data.get("error")
                    or (failure.get("reason") if isinstance(failure, dict) else None)
                    or "unrecorded"
                )
                reason = " ".join(str(reason).split())[:300]
                echo(
                    f"PLACEMENT lane={lane} status={status} host={host} reason={reason} receipt={receipt}"
                )
                code = data.get("exit_code")
                return (
                    code if type(code) is int and code != 0 else rt["EXIT_LANE_ERROR"]
                )
            if status in ("preparing", "running"):
                echo(
                    f"PLACEMENT lane={lane} status=running host={host} receipt={receipt}"
                )
                return cast("int", rt["EXIT_OK"])
            if dead:
                echo(
                    f"PLACEMENT lane={lane} status=dead receipt={receipt}: the runner exited before placement"
                )
                return cast("int", rt["EXIT_REMOTE_FAILED"])
            if not alive():
                dead = True  # Re-read the child's last receipt before declaring it dead (OMN-20840).
                continue
            if clock() >= end:
                echo(
                    f"PLACEMENT lane={lane} status=placing after {int(max_s)}s; read it with: "
                    f"onex-remote-lane wait --receipt {receipt}"
                )
                return cast("int", rt["EXIT_STILL_RUNNING"])
            sleep(min(poll_s, max(0.0, end - clock())))

    def until_placed(self, call: Callable[[], int], receipt_path: Path) -> int:
        """Run the lane (``call``); any exit that escapes it before placement ends the receipt (``end_unplaced``) and goes on."""
        try:
            return call()
        except BaseException as exc:
            self.end_unplaced(receipt_path, exc)
            raise

    def end_unplaced(
        self, receipt_path: Path, exc: BaseException
    ) -> dict[str, Any] | None:
        """End a receipt still ``placing`` and never final with the reason the runner stopped (a SIGTERM exits through
        SystemExit 143; any uncaught error). A receipt already final, or past placing, is left to the runner's ``finish``."""
        try:
            data = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if (
            not isinstance(data, dict)
            or data.get("final")
            or data.get("status") != "placing"
        ):
            return None
        code = (
            exc.code
            if isinstance(exc, SystemExit) and type(exc.code) is int and exc.code
            else self.runtime.get("EXIT_REMOTE_FAILED", LOST_EXIT)
        )
        why = "SIGTERM" if code == SIGTERM_EXIT else f"{type(exc).__name__}: {exc}"
        data.update(
            status="interrupted",
            final=True,
            exit_code=code,
            ended_at=self.runtime["_now"](),
            error=f"REMOTE-LANE-INTERRUPTED the runner stopped before the lane was placed ({why})"[
                :400
            ],
        )
        self.runtime["write_receipt"](receipt_path, data)
        return data

    # ---- the throughput tick -------------------------------------------------------------------------------------------

    def without_queued_no_host(
        self, receipts: list[dict[str, Any]], entries: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """The receipts the dispatch finding reads, less each queued lane's no-host and placement-lost ones: a lane waiting in
        the queue is not a lost dispatch, and the queue line reports it (one line for all of them, not one MISSING each)."""
        waiting = {str(e.get("lane")) for e in entries}
        return [
            r
            for r in receipts
            if not (
                r.get("lane") in waiting and r.get("status") in ("no-host", LOST_STATUS)
            )
        ]

    def placement_refusals(
        self,
        receipts: list[dict[str, Any]],
        hosts: list[Any],
        lanes: Mapping[str, int],
        now: datetime,
        *,
        window_s: float = PLACEMENT_REFUSAL_WINDOW_S,
    ) -> dict[str, str]:
        """Host name -> why the runner's newest placement read of it inside ``window_s`` refused a lane, as the tick says it.
        ``receipts`` are the tick's (``started_at``, ``readings``: the runner's ``Reading.describe`` lines); ``lanes`` the lanes
        placed per host, which the reserved share counts."""
        lp, parse = self.runtime["_load_placement"](), self.runtime["lc"].parse_time
        by_name = {host.name: host for host in hosts}
        newest: dict[str, tuple[datetime, str]] = {}
        for receipt in receipts:
            started = parse(receipt.get("started_at"))
            if started is None or not 0 <= (now - started).total_seconds() <= window_s:
                continue
            for text in receipt.get("readings") or []:
                name = str(text).partition(":")[0]
                if name in by_name and (
                    name not in newest or started > newest[name][0]
                ):
                    newest[name] = (started, str(text))
        out: dict[str, str] = {}
        for name, (started, text) in newest.items():
            reading = (
                None if lp is None else lp.Reading.from_description(by_name[name], text)
            )
            why = (
                None
                if reading is None
                else replace(
                    reading, lanes_placed=int(lanes.get(name, 0))
                ).lane_admission_refusal
            )
            if why:
                out[name] = (
                    f"PLACEMENT-REFUSED at {_stamp(started)} (the runner's read): {why}"
                )
        return out

    def requeue_lost_placements(
        self,
        now: datetime,
        *,
        lanes_dir: Path | None = None,
        directory: Path | None = None,
        replace: bool = True,
        alive: Callable[[int], bool] | None = None,
    ) -> list[tuple[str, str, str, str]]:
        """End every receipt left ``placing`` by a runner that is no longer alive, and re-queue its lane when it was lost inside
        the lane queue's term (``LANE_QUEUE_TTL_S``) and is neither queued already nor superseded by a later run of the same
        lane. The receipt becomes final with status ``placement-lost`` and the reason; a lane lost longer ago is ended with the
        reason and counted on one line. ``replace=False`` (a read-only tick) changes nothing and reports each recent lost lane
        as MISSING. Returns the tick's (level, loop, why, fix) findings."""
        rt = self.runtime
        lp = rt["_load_placement"]()
        if lp is None:
            return []
        alive = alive or getattr(lp, PID_ALIVE_ATTR)
        lanes_dir = lanes_dir or Path(
            os.environ.get("ONEX_REMOTE_LANE_DIR")
            or Path.home() / ".local/state/omni/remote-lanes"
        )
        directory = directory or lp.placement_dir()
        receipts = []
        for path in lanes_dir.glob("*/*.json"):
            data = rt["_read_json"](path) if not path.name.endswith(".job.json") else {}
            started = rt["lc"].parse_time(data.get("started_at"))
            if started is not None:
                receipts.append((started, path, data))
        queued = {str(e.get("lane")) for e in lp.lane_queue_entries(directory)}
        term_h = f"{lp.LANE_QUEUE_TTL_S / 3600:g}h"
        out: list[tuple[str, str, str, str]] = []
        stale: list[str] = []
        for started, path, data in sorted(receipts, key=lambda item: item[0]):
            lane, run_id, pid = (
                str(data.get("lane") or path.parent.name),
                str(data.get("run_id") or path.stem),
                data.get("pid"),
            )
            if (
                data.get("final")
                or data.get("status") != "placing"
                or type(pid) is not int
            ):
                continue
            if (now - started).total_seconds() < LOST_GRACE_S or alive(pid):
                continue
            later = next(
                (
                    str(o.get("run_id") or p.stem)
                    for s, p, o in receipts
                    if s > started and o.get("lane") == lane
                ),
                None,
            )
            recent = (now - started).total_seconds() < lp.LANE_QUEUE_TTL_S
            reason = f"the runner (pid {pid}) exited during placement, started {_stamp(started)}, with no final receipt"
            if not replace:
                if recent and later is None and lane not in queued:
                    out.append(
                        (
                            "missing",
                            f"dispatch-lost:{lane}",
                            f"run {run_id}: {reason}; nothing will start it",
                            "run the tick without --no-replace, which re-queues it, or re-send the lane",
                        )
                    )
                continue
            requeue = (
                recent
                and later is None
                and lane not in queued
                and bool(data.get("brief"))
            )
            if requeue:
                argv = rt["_relaunch_argv"](
                    Path("runner"),
                    data,
                    rt["_read_json"](path.with_name(f"{run_id}.job.json")),
                )[3:]
                kept = data.get("brief_copy")
                if (
                    kept and Path(kept).is_file()
                ):  # the brief the run pinned, safe from a rewrite of the dispatcher's file
                    argv[argv.index("--brief") + 1] = str(kept)
                try:
                    lp.lane_queue_enqueue(
                        directory,
                        lane,
                        argv=argv,
                        model=str(data.get("model") or ""),
                        priority_class=str(
                            data.get("priority_class") or lp.PRIORITY_OTHER
                        ),
                        pin=None,
                        run_id=run_id,
                        reason=reason[:400],
                        now=now,
                    )
                except (OSError, ValueError) as exc:
                    out.append(
                        (
                            "missing",
                            f"dispatch-lost:{lane}",
                            f"run {run_id}: {reason}; the lane queue is unwritable: {exc}",
                            "re-send the lane by hand",
                        )
                    )
                    continue
                queued.add(lane)
            why = (
                f"{reason}; re-queued"
                if requeue
                else f"{reason}; superseded by run {later}"
                if later
                else f"{reason}; older than the lane queue's {term_h} term, not re-queued"
                if not recent
                else f"{reason}; the lane queue already holds the lane"
                if lane in queued
                else f"{reason}; the receipt names no brief, so it cannot be re-queued"
            )
            data.update(
                status=LOST_STATUS,
                final=True,
                exit_code=LOST_EXIT,
                ended_at=_stamp(now),
                reason=why,
                lost_runner={"pid": pid, "found_at": _stamp(now), "requeued": requeue},
            )
            try:
                rt["_write_json_atomic"](path, data)
            except OSError as exc:
                out.append(
                    (
                        "unknown",
                        f"dispatch-lost:{lane}",
                        f"cannot end receipt {path}: {exc}",
                        "make the receipt writable",
                    )
                )
                continue
            if requeue:
                out.append(
                    (
                        "note",
                        f"dispatch-lost:{lane}",
                        f"dispatch-lost:{lane} re-queued: run {run_id}: {reason}",
                        "",
                    )
                )
            elif not recent:
                stale.append(lane)
            elif not later and lane not in queued:
                out.append(
                    (
                        "missing",
                        f"dispatch-lost:{lane}",
                        f"run {run_id}: {why}",
                        "re-send the lane by hand",
                    )
                )
        if stale:
            more = ", ..." if len(stale) > 8 else ""
            out.append(
                (
                    "note",
                    "dispatch-lost",
                    f"dispatch-lost: ended {len(stale)} receipt(s) left placing by a dead runner "
                    f"more than {term_h} ago ({', '.join(stale[:8])}{more})",
                    "",
                )
            )
        return out
