# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Admission and slot cap of one lab host from its measured capacity (OMN-23337).

Fixed numbers misread hosts: at 2026-11-09 the focused test run refused every Docker host at a fixed
one-minute load of 8, a 32-core host at load 34.5 and a many-core server at 12.7 to 16.5 alike, and the
floor counted slots from fixed per-host caps. Here the bars are per logical core and per share of total
memory, and the cap follows headroom:

- ``cpu_headroom_slots``: the load the host may still take at ``slot_load_per_core``, in units of
  ``cores_per_slot``.
- ``memory_headroom_slots``: the memory above ``min_free_memory_ratio`` of the total, in units of
  ``memory_per_slot_gb``.
- swap activity over either swap bar refuses the host.
- A host that passes every bar has ``free_slots`` of the smaller headroom, at least one; a refused host
  has none, whatever the floor says, so admission and the floor's slots number are one answer.
  ``slot_cap`` is the running lanes plus the headroom (refused or not), held between the floor and the
  ceiling; a host whose cap is already full is refused naming the cap.
"""

from __future__ import annotations

import math

from omnimarket.models.model_host_capacity import (
    ModelHostCapacityRequest,
    ModelHostCapacityVerdict,
)


class HandlerHostCapacity:
    def handle(self, request: ModelHostCapacityRequest) -> ModelHostCapacityVerdict:
        r, lim = request.reading, request.limits
        load_per_core = r.load_one_minute / r.logical_cores
        free_ratio = r.mem_avail_gb / r.mem_total_gb
        reasons: list[str] = []

        if load_per_core > lim.max_load_per_core:
            reasons.append(
                f"load1 {r.load_one_minute:.1f} on {r.logical_cores} cores is "
                f"{load_per_core:.2f} per core > {lim.max_load_per_core}"
            )
        if free_ratio < lim.min_free_memory_ratio:
            reasons.append(
                f"free memory {r.mem_avail_gb:.1f}G of {r.mem_total_gb:.1f}G is "
                f"{free_ratio:.2f} < {lim.min_free_memory_ratio}"
            )
        swapping = False
        if r.swap_in_kb_s is not None and r.swap_out_kb_s is not None:
            if r.swap_in_kb_s + r.swap_out_kb_s > lim.max_swap_kb_s:
                swapping = True
                reasons.append(
                    f"swap traffic {r.swap_in_kb_s + r.swap_out_kb_s:.0f} KB/s "
                    f"> {lim.max_swap_kb_s:.0f} KB/s"
                )
            if r.swap_in_kb_s > lim.max_swap_in_kb_s:
                swapping = True
                reasons.append(
                    f"swap-in {r.swap_in_kb_s:.0f} KB/s > {lim.max_swap_in_kb_s:.0f} KB/s (paging)"
                )

        cpu_room = max(
            0.0, lim.slot_load_per_core * r.logical_cores - r.load_one_minute
        )
        cpu_slots = math.floor(cpu_room / lim.cores_per_slot)
        mem_room = max(0.0, r.mem_avail_gb - lim.min_free_memory_ratio * r.mem_total_gb)
        mem_slots = math.floor(mem_room / lim.memory_per_slot_gb)
        # The cap is what the host's measured headroom holds, refused or not. A host that passes every bar has room for one
        # more lane whatever the arithmetic rounds to; one that does not has none free.
        headroom = 0 if swapping else min(cpu_slots, mem_slots)
        if not reasons:
            headroom = max(1, headroom)
        binding = "swap" if swapping else "cpu" if cpu_slots <= mem_slots else "memory"

        bounds = lim.hosts.get(r.host)
        floor = bounds.slot_floor if bounds else lim.slot_floor
        ceiling = (
            bounds.slot_ceiling
            if bounds
            else max(floor, math.floor(lim.slot_ceiling_per_core * r.logical_cores))
        )
        cap = min(ceiling, max(floor, r.running_lanes + headroom))
        free = 0 if reasons else max(0, cap - r.running_lanes)
        if not reasons and free == 0:
            reasons.append(f"slot cap {cap} reached by {r.running_lanes} running lanes")
            binding = "ceiling" if cap == ceiling else "floor"
        elif cap == ceiling and headroom > 0:
            binding = "ceiling"
        return ModelHostCapacityVerdict(
            host=r.host,
            admitted=not reasons,
            refusals=tuple(reasons),
            load_per_core=load_per_core,
            free_memory_ratio=free_ratio,
            cpu_headroom_slots=cpu_slots,
            memory_headroom_slots=mem_slots,
            binding=binding,
            slot_cap=cap,
            slot_floor=floor,
            slot_ceiling=ceiling,
            free_slots=free,
            inputs=request,
        )
