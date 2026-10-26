"""A lab host the tick cannot trust is named, once per episode (OMN-17427).

One day a pool host's read printed ``<host>:UNREADABLE(rc=255 timed out after 20s)``: the host (32 cores, lane
cap 16) was swap-thrashing (74 GB swap used, swap-in about 85 MB/s, IO wait 23 to 40 percent, load about 40). The runner
and the throughput tick treat an unreadable host as zero slots, the same as a full one, and no tick line, push or
exception row said so; the operator found out by asking (asking why the host was not working and why nobody knew yet).

Two findings, both MISSING, both naming the host, its lane cap, what the probe said and a FIX:

``host-unreadable:<host>``  the host's pool read failed on two consecutive ticks (one failure is a flap and prints
                            nothing; a host the shared table marks down is that table's state and prints nothing).
``host-swapping:<host>``    the probe answered and its own readings show swap-in over ``SWAP_IN_BOUND_KB_S``, memory
                            PSI over ``MEM_PSI_BOUND`` or the macOS critical memory pressure. The admission bar now
                            refuses swap-in over the same bound (``ADMIT_MAX_SWAP_IN_KB_S``, OMN-21288).

Each alarm is one decision row in the exception queue (the tick's existing attention path; the queue reads a repeat of an
open row as the same case), so the operator is pushed once per host per episode and not once per tick. An episode ends
after two clean reads of the host (answered, and not swapping), which resolves its rows; a flapping host stays one
episode. The host's ``MISSING lab-headroom`` line is silenced for the tick (``ModelHostPoolAlarm.silenced``), so neither
the Stop idle guard nor the lab fill reads its slots as free headroom.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import (
    MEM_PRESSURE_CRITICAL,
    MEM_PSI_BOUND,
    SWAP_IN_BOUND_KB_S,
    ModelHostPoolAlarm,
    ModelHostPoolAlarmRequest,
    ModelHostPoolReading,
)

TICKET = "OMN-17427"
STATE_FILE = "host-pool-alarm.json"
CONSECUTIVE = 2  # unreadable reads in a row before the alarm; clean reads in a row before the episode ends
CLASS_UNREADABLE, CLASS_SWAPPING = "host-unreadable", "host-swapping"
SOURCE = "throughput-tick host pool read"
INSPECT = (
    "read its load, memory, swap and sshd from the launching host (uptime; free -m; vmstat 1 5; "
    "systemctl status sshd; ps by RSS) through the lab table's access path"
)
FIX_UNREADABLE = (
    f"{INSPECT}; the runner counts the host as zero slots, the same as full, so no lane is placed there until a read "
    "returns, and a host that answers the probe in over 20 s is paging; free the memory before placing anything"
)
FIX_SWAPPING = (
    f"{INSPECT}; find what holds the memory and let it drain, since the host still admits lanes under its swap bar "
    "while it pages; the tick treats its slots as taken until two clean reads"
)


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _one_line(value: object, limit: int) -> str:
    return " ".join(str(value).replace("|", "/").split())[:limit]


def read_streaks(path: Path) -> dict[str, dict[str, Any]]:
    """The per-host streaks the last tick left; an absent or unreadable file is none (the alarm then needs two more reads)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        hosts = data["hosts"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    return (
        {str(k): v for k, v in hosts.items() if isinstance(v, dict)}
        if isinstance(hosts, dict)
        else {}
    )


def write_streaks(path: Path, hosts: dict[str, dict[str, Any]]) -> str | None:
    """Replace the file atomically; the reason it could not be written, else None."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}")
        tmp.write_text(json.dumps({"hosts": hosts}, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        return _one_line(exc, 160)
    return None


def swap_reasons(reading: ModelHostPoolReading) -> list[str]:
    """What in the probe's own readings says the host is paging or short of memory (the fields the pool line prints)."""
    reasons = []
    if reading.swap_in_kb_s is not None and reading.swap_in_kb_s > SWAP_IN_BOUND_KB_S:
        reasons.append(
            f"swap-in {reading.swap_in_kb_s / 1000:.0f} MB/s over {SWAP_IN_BOUND_KB_S / 1000:.0f} MB/s"
        )
    if reading.mem_psi_avg60 is not None and reading.mem_psi_avg60 > MEM_PSI_BOUND:
        reasons.append(
            f"memory PSI avg60 {reading.mem_psi_avg60:.2f} over {MEM_PSI_BOUND:g}"
        )
    if (
        reading.mem_pressure is not None
        and reading.mem_pressure >= MEM_PRESSURE_CRITICAL
    ):
        reasons.append(f"memory pressure level {reading.mem_pressure} (critical)")
    return reasons


def probe_cells(reading: ModelHostPoolReading) -> str:
    """The probe fields as the pool line prints them: load1, swap, si, so, mempsi, pressure."""
    cells = []
    if reading.load1 is not None:
        cells.append(f"load1={reading.load1:.1f}")
    if reading.swap_used_frac is not None:
        cells.append(f"swap={reading.swap_used_frac * 100:.0f}%")
    if reading.swap_in_kb_s is not None:
        cells.append(f"si={reading.swap_in_kb_s:.0f}KB/s")
    if reading.swap_out_kb_s is not None:
        cells.append(f"so={reading.swap_out_kb_s:.0f}KB/s")
    if reading.mem_psi_avg60 is not None:
        cells.append(f"mempsi={reading.mem_psi_avg60:.2f}")
    if reading.mem_pressure is not None:
        cells.append(f"pressure={reading.mem_pressure}")
    return ",".join(cells)


class HandlerHostPoolAlarm:
    def handle(self, request: ModelHostPoolAlarmRequest) -> ModelHostPoolAlarm:
        if not request.readings:
            return ModelHostPoolAlarm(
                [], frozenset()
            )  # no pool was read this tick: nothing can be judged, nothing is reset
        path = request.state_dir / STATE_FILE
        streaks = read_streaks(path)
        found: list[tuple[str, str, str, str]] = []
        silenced: set[str] = set()
        attention: list[tuple[str, ModelHostPoolReading, str, str, str]] = []
        resolve: list[tuple[str, list[str]]] = []
        now = _stamp(request.now)
        for reading in request.readings:
            host = reading.host
            if not reading.available:
                streaks.pop(
                    host, None
                )  # the table marks it down: that is its state, and no read is expected
                continue
            before = streaks.get(host, {})
            open_classes = [
                c
                for c in before.get("open", [])
                if c in (CLASS_UNREADABLE, CLASS_SWAPPING)
            ]
            unreadable = (
                int(before.get("unreadable", 0)) + 1 if reading.error is not None else 0
            )
            reasons = [] if reading.error is not None else swap_reasons(reading)
            clean = (
                0
                if reading.error is not None or reasons
                else int(before.get("clean", 0)) + 1
            )
            since = str(before.get("since") or now) if unreadable > 1 else now
            if unreadable >= CONSECUTIVE:
                why = (
                    f"{host} (lane cap {reading.lane_cap}) pool read UNREADABLE on {unreadable} consecutive ticks "
                    f"since {since}: {_one_line(reading.error, 160)}"
                )
                found.append(
                    ("missing", f"{CLASS_UNREADABLE}:{host}", why, FIX_UNREADABLE)
                )
                silenced.add(host)
                attention.append((CLASS_UNREADABLE, reading, why, FIX_UNREADABLE, host))
            elif reading.error is None and reasons:
                why = (
                    f"{host} (lane cap {reading.lane_cap}) answers the probe but is paging: {'; '.join(reasons)} "
                    f"({probe_cells(reading)})"
                )
                found.append(("missing", f"{CLASS_SWAPPING}:{host}", why, FIX_SWAPPING))
                silenced.add(host)
                attention.append((CLASS_SWAPPING, reading, why, FIX_SWAPPING, host))
            if clean >= CONSECUTIVE and open_classes:
                resolve.append((host, open_classes))
                open_classes = []
            streaks[host] = {
                "unreadable": unreadable,
                "since": since,
                "clean": clean,
                "open": open_classes,
            }
        if not request.emit:
            return ModelHostPoolAlarm(found, frozenset(silenced))
        found = self._attention(request, attention, resolve, streaks, found)
        error = write_streaks(path, streaks)
        if error is not None:
            found.append(
                (
                    "unknown",
                    "host-pool-alarm",
                    f"cannot write {path}: {error}",
                    "make the state directory writable (OMNI_SESSION_START_STATE_DIR names another)",
                )
            )
        return ModelHostPoolAlarm(found, frozenset(silenced))

    def _attention(
        self,
        request: ModelHostPoolAlarmRequest,
        attention: list[tuple[str, ModelHostPoolReading, str, str, str]],
        resolve: list[tuple[str, list[str]]],
        streaks: dict[str, dict[str, Any]],
        found: list[tuple[str, str, str, str]],
    ) -> list[tuple[str, str, str, str]]:
        """One decision row per host and class while the episode lasts; a recovered host's rows resolved. A queue that
        cannot be written is one UNKNOWN: the MISSING line stands, and the next tick tries again."""
        from omnimarket.nodes.node_exception_queue_effect import handler as queue
        from omnimarket.nodes.node_exception_queue_effect import models as queue_models

        where = request.exception_dir or queue.state_dir()
        now = _stamp(request.now)
        rows: dict[str, str] = {}  # loop -> what the queue did with its row
        try:
            if not request.plugin_repo:
                raise ValueError(
                    "plugin_repo is not set; supply the repos overlay's plugin_source"
                )
            for cls, _reading, why, fix, host in attention:
                row = queue_models.ModelExceptionRow(
                    cls=cls,
                    key=host,
                    source=SOURCE,
                    evidence=_one_line(why, 400),
                    tried="the tick printed the MISSING line and treats the host's slots as taken; it changes nothing on the host",
                    proposed_fix=_one_line(fix, 400),
                    decision=True,
                    ticket=TICKET,
                    repo=request.plugin_repo,
                )
                done = queue.HandlerExceptionEmit().handle(
                    queue_models.ModelExceptionEmitRequest(
                        state_dir=where,
                        row=row,
                        now=now,
                        runner=request.runner,
                        parent=request.parent,
                    )
                )
                if done.status == "refused":
                    raise ValueError(done.detail)
                rows[f"{cls}:{host}"] = f"{done.status} {done.id}"
                open_classes = streaks[host].setdefault("open", [])
                if cls not in open_classes:
                    open_classes.append(cls)
            if resolve:
                decisions = (
                    queue.HandlerExceptionQueue()
                    .handle(queue_models.ModelExceptionQueueRequest(where, "operator"))
                    .rows
                )
                for host, classes in resolve:
                    for queued in decisions:
                        if queued["key"] == host and queued["cls"] in classes:
                            queue.HandlerExceptionResolve().handle(
                                queue_models.ModelExceptionResolveRequest(
                                    where,
                                    queued["id"],
                                    "throughput-tick",
                                    f"{host} read clean on {CONSECUTIVE} consecutive ticks",
                                    now,
                                )
                            )
        except (OSError, ValueError, KeyError) as exc:
            found = [
                *found,
                (
                    "unknown",
                    "host-pool-attention",
                    f"cannot write the exception queue {where}: {_one_line(exc, 160)}",
                    "make the exception queue directory writable (ONEX_STATE_DIR names it)",
                ),
            ]
        return [
            (
                level,
                loop,
                f"{why} | attention: exception queue {rows[loop]}"
                if loop in rows
                else why,
                fix,
            )
            for level, loop, why, fix in found
        ]


def split(facts: dict[str, Any]) -> tuple[dict[str, Any], list[ModelHostPoolReading]]:
    """The tick's live admission facts without the probe readings the pool read left in ``pool`` (the decision node's wire
    model takes no such key), and each reading as the alarm's typed input. Facts with no ``pool`` (a placement that could
    not be read) give none, and an alarm with no reading judges nothing."""
    pool = facts.pop("pool", None) or []
    unavailable = set(facts.get("unavailable_hosts") or [])
    return facts, [
        ModelHostPoolReading(
            r.host.name,
            r.lane_cap,
            r.error,
            r.host.name not in unavailable,
            r.load1,
            r.swap_used_frac,
            r.swap_in_kb_s,
            r.swap_out_kb_s,
            r.mem_psi_avg60,
            r.mem_pressure,
        )
        for r in pool
    ]


def alarm(
    result: dict[str, Any],
    request: dict[str, Any],
    pool: list[ModelHostPoolReading],
    now: datetime,
    state_dir: Path,
    runner: Path | None,
    parent: str | None,
    read_only: bool,
    fold: Callable[[dict[str, Any], list[tuple[str, str, str, str]]], dict[str, Any]],
) -> dict[str, Any]:
    """The tick's call: the alarm's findings folded into its result (``fold`` is the tick's own ``add_findings``). A host
    alarmed has its ``MISSING lab-headroom`` line dropped and every engine's slots zeroed in ``request``, so the lab fill,
    the lane queue and the Stop idle guard (which reads the printed line) do not read them as free. A read-only tick reads
    the streaks and writes nothing."""
    found = HandlerHostPoolAlarm().handle(
        ModelHostPoolAlarmRequest(
            pool, now, state_dir, emit=not read_only, runner=runner, parent=parent
        )
    )
    if not found.silenced:
        return fold(result, found.found)
    for host in (request.get("lab_headroom") or {}).get("hosts") or []:
        if host.get("name") in found.silenced:
            slots = (
                host.get("admission") or {}
            )  # GLM's too: the node's wire model never saw them
            host["admission"] = {
                **slots,
                **{
                    k: 0
                    for k in {*slots, "slots"}
                    if k == "slots" or k.endswith("_slots")
                },
            }
    quiet = tuple(f"MISSING lab-headroom:{name} " for name in found.silenced)
    missing = [
        loop
        for loop in result.get("missing") or []
        if loop not in {f"lab-headroom:{n}" for n in found.silenced}
    ]
    return fold(
        {
            **result,
            "lines": [x for x in result["lines"] if not x.startswith(quiet)],
            "missing": missing,
        },
        found.found,
    )
