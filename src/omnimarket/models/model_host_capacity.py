# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed readings, overlay limits and verdict for the host capacity node (OMN-23337).

A lab host is judged on what it measured: load per logical core and free memory as a share of its
total, never an absolute load number. Every limit arrives from the ``host_capacity`` overlay; the
models carry no default for any of them.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelHostSlotBounds(BaseModel):
    """One host's own slot floor and ceiling, when the overlay names the host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    slot_floor: int = Field(ge=0)
    slot_ceiling: int = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> ModelHostSlotBounds:
        if self.slot_ceiling < self.slot_floor:
            raise ValueError("slot_ceiling is under slot_floor")
        return self


class ModelHostCapacityLimits(BaseModel):
    """The ``host_capacity`` overlay: every bar and bound, held as data.

    ``max_load_per_core`` and ``min_free_memory_ratio`` are the admission bars; ``slot_load_per_core`` is
    the load per core the slot headroom is counted up to (never under the bar). ``cores_per_slot`` and
    ``memory_per_slot_gb`` are what one further lane is charged. A host's slot cap is its running lanes
    plus the slots its measured headroom still holds, held between ``slot_floor`` and the ceiling
    (``slot_ceiling_per_core`` of its logical cores, or the host's own ``hosts`` entry).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_load_per_core: float = Field(gt=0)
    slot_load_per_core: float = Field(gt=0)
    min_free_memory_ratio: float = Field(ge=0, lt=1)
    max_swap_kb_s: float = Field(gt=0)
    max_swap_in_kb_s: float = Field(gt=0)
    cores_per_slot: float = Field(gt=0)
    memory_per_slot_gb: float = Field(gt=0)
    slot_floor: int = Field(ge=0)
    slot_ceiling_per_core: float = Field(gt=0)
    hosts: dict[str, ModelHostSlotBounds] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _slot_load_not_under_the_bar(self) -> ModelHostCapacityLimits:
        if self.slot_load_per_core < self.max_load_per_core:
            raise ValueError("slot_load_per_core is under max_load_per_core")
        return self


class ModelHostCapacityReading(BaseModel):
    """One host's capacity reading: logical cores, memory, swap activity and load.

    ``swap_in_kb_s`` and ``swap_out_kb_s`` are None when the probe did not return them; a reading that
    is None is never judged. ``running_lanes`` is what the host already carries.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    logical_cores: int = Field(gt=0)
    mem_total_gb: float = Field(gt=0)
    mem_avail_gb: float = Field(ge=0)
    swap_in_kb_s: float | None = None
    swap_out_kb_s: float | None = None
    load_one_minute: float = Field(ge=0)
    running_lanes: int = Field(ge=0)


class ModelHostCapacityRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reading: ModelHostCapacityReading
    limits: ModelHostCapacityLimits


class ModelHostCapacityVerdict(BaseModel):
    """Admission and slot cap from one reading, with every input that produced them.

    ``admitted`` is true exactly when ``free_slots`` is at least one: the floor's slots number and the
    admission are the same answer. ``inputs`` is the request itself, so a recorded verdict carries the
    reading and the limits it was computed from.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    admitted: bool
    refusals: tuple[str, ...]
    load_per_core: float
    free_memory_ratio: float
    cpu_headroom_slots: int
    memory_headroom_slots: int
    binding: str
    slot_cap: int
    slot_floor: int
    slot_ceiling: int
    free_slots: int
    inputs: ModelHostCapacityRequest

    def record(self) -> str:
        """One line for a reading row: the cap and the inputs it came from."""
        r, lim = self.inputs.reading, self.inputs.limits
        swap = (
            "unread"
            if r.swap_in_kb_s is None or r.swap_out_kb_s is None
            else f"{r.swap_in_kb_s:.0f}/{r.swap_out_kb_s:.0f}"
        )
        return (
            f"capacity host={self.host} cap={self.slot_cap} free={self.free_slots} "
            f"running={r.running_lanes} floor={self.slot_floor} ceiling={self.slot_ceiling} "
            f"binding={self.binding} cores={r.logical_cores} load1={r.load_one_minute:.2f} "
            f"load_per_core={self.load_per_core:.3f}/{lim.max_load_per_core} "
            f"mem_avail_gb={r.mem_avail_gb:.1f}/{r.mem_total_gb:.1f} "
            f"free_mem_ratio={self.free_memory_ratio:.3f}/{lim.min_free_memory_ratio} "
            f"swap_kb_s_in/out={swap} admitted={str(self.admitted).lower()}"
        )
