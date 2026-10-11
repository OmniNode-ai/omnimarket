# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How many lanes each lab host can carry for one lab-fill run (OMN-20668)."""

from __future__ import annotations

import math

from ..models import (
    ModelLabFillCapacityRequest,
    ModelLabFillCapacityResult,
    ModelLabFillHeadroomPolicy,
    ModelLabFillHostCapacity,
)
from .helpers_js_value import UNDEFINED, get, js_str, num, round_half_up, truthy


def host_capacity(
    reading: object, policy: ModelLabFillHeadroomPolicy
) -> ModelLabFillHostCapacity:
    """One host's lanes: the smaller of its real idle and the runner's own slots.

    Real idle (the busy-core sample when the probe read one, else load1) and free
    memory decide how many lanes the host could carry; the runner's lane slots decide
    how many it will admit. A host the runner refuses, a limited host, an unreadable
    host, one with no engine login and the launching host get none.
    """
    name = get(reading, "name")
    running_lanes = get(reading, "running_lanes")
    out = {
        "name": name if isinstance(name, str) else "?",
        "lanes": 0,
        "idle_lanes": 0,
        "runner_slots": 0,
        "cap_bound": False,
        "codex": truthy(get(reading, "codex_ok")),
        "claude": truthy(get(reading, "login_claude")),
        "running": len(running_lanes) if isinstance(running_lanes, list) else 0,
        "load_per_core": None,
        "reason": "",
    }
    if isinstance(reading, list):
        # An array is an object with none of a reading's keys, so it is incomplete, not absent.
        return ModelLabFillHostCapacity(**{**out, "reason": "incomplete-reading"})
    if not isinstance(reading, dict):
        return ModelLabFillHostCapacity(**{**out, "reason": "no-reading"})
    sampled_cores = num(reading.get("cores", UNDEFINED))
    sampled_load = num(reading.get("load1", UNDEFINED))
    sampled_busy = num(reading.get("busy_cores", UNDEFINED))
    if sampled_cores is not None and sampled_cores > 0 and sampled_load is not None:
        sampled_used = (
            sampled_load if sampled_busy is None else min(sampled_load, sampled_busy)
        )
        out["load_per_core"] = round_half_up(sampled_used / sampled_cores * 100) / 100
    if truthy(reading.get("local")):
        return ModelLabFillHostCapacity(**{**out, "reason": "launching-host"})
    if truthy(reading.get("error")):
        return ModelLabFillHostCapacity(
            **{**out, "reason": f"unreadable: {js_str(reading['error'])[:60]}"}
        )
    if truthy(reading.get("limited")):
        return ModelLabFillHostCapacity(
            **{**out, "reason": f"limited until {js_str(reading['limited'])}"}
        )
    if truthy(reading.get("refusal")):
        return ModelLabFillHostCapacity(
            **{**out, "reason": f"runner-refused: {js_str(reading['refusal'])[:80]}"}
        )
    cores = num(reading.get("cores", UNDEFINED))
    load1 = num(reading.get("load1", UNDEFINED))
    mem = num(reading.get("mem_avail_gb", UNDEFINED))
    if cores is None or cores <= 0 or load1 is None or mem is None:
        return ModelLabFillHostCapacity(**{**out, "reason": "incomplete-reading"})
    busy = num(reading.get("busy_cores", UNDEFINED))
    used = load1 if busy is None else min(load1, busy)
    load_per_core = round_half_up(used / cores * 100) / 100
    idle_cores = policy.max_busy_per_core * cores - used
    by_cores = math.floor(idle_cores / policy.lane_cores) if idle_cores > 0 else 0
    by_mem = (
        math.floor((mem - policy.mem_headroom_gb) / policy.lane_mem_gb)
        if mem > policy.mem_headroom_gb
        else 0
    )
    idle_lanes = max(0, min(by_cores, by_mem))
    slots = num(reading.get("runner_slots", UNDEFINED)) or 0
    runner_slots = max(0, math.floor(slots))
    lanes = min(idle_lanes, runner_slots, policy.max_per_host_per_run)
    reason = ""
    if not lanes:
        if not idle_lanes:
            reason = "memory" if by_cores else "load"
        elif not runner_slots:
            reason = "runner-cap"
    shared = {
        **out,
        "load_per_core": load_per_core,
        "idle_lanes": idle_lanes,
        "runner_slots": runner_slots,
        "cap_bound": idle_lanes > runner_slots,
    }
    if not out["claude"] and not out["codex"] and lanes:
        return ModelLabFillHostCapacity(**{**shared, "reason": "no-engine-login"})
    return ModelLabFillHostCapacity(**{**shared, "lanes": lanes, "reason": reason})


class HandlerLabFillCapacity:
    """Plan the lanes every lab host can carry from the runner's placement readings."""

    def handle(
        self, request: ModelLabFillCapacityRequest
    ) -> ModelLabFillCapacityResult:
        hosts = [host_capacity(reading, request.policy) for reading in request.readings]
        lab = tuple(h for h in hosts if h.reason != "launching-host")
        free = sum(h.lanes for h in lab)
        return ModelLabFillCapacityResult(
            hosts=lab, free=free, budget=min(request.max_lanes, free)
        )
