# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The limits a lane's commands run under on a lab host (OMN-22848, plan task T15).

The remote-lane runner script resolves these before it places, claims or ships anything (``lane_guard.resolve_limits`` and the
``Limits`` constructor of ``lane_guard_host``), and a refusal ends the run with the usage status and no CLAIM. This is that
resolution as a pure node: the same arithmetic, the same refusal texts, and every bound arrives on the request from the caller's
overlay instead of a module constant.
"""

import re

from ..models.model_remote_lane_guard_limits import (
    ModelGuardBounds,
    ModelGuardLimits,
    ModelRemoteLaneGuardLimitsRequest,
    ModelRemoteLaneGuardLimitsVerdict,
)

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)(?:i?B)?\s*$", re.IGNORECASE)
_SIZE_UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
_GIB = 1024**3
_MIB = 1024**2


def parse_size(text: str) -> int:
    """``8G``, ``512M``, ``8589934592`` to bytes; ValueError for anything else."""
    found = _SIZE_RE.match(str(text))
    if found is None:
        raise ValueError(f"{text!r} is not a size (use 8G, 512M or a byte count)")
    return int(float(found.group(1)) * _SIZE_UNITS[found.group(2).upper()])


def _limits(
    runtime_s: int, mem_bytes: int, cpu_pct: int, kind: str, bounds: ModelGuardBounds
) -> ModelGuardLimits:
    """The checks of the host program's ``Limits`` constructor, in its order."""
    if kind not in bounds.kinds:
        raise ValueError(f"kind {kind!r} is not one of {tuple(bounds.kinds)}")
    if not 1 <= runtime_s <= bounds.hard_cap_s:
        raise ValueError(
            f"runtime {runtime_s}s is outside 1..{bounds.hard_cap_s}s (the hard cap is {bounds.hard_cap_s // 3600} h)"
        )
    if not bounds.mem_floor_bytes <= mem_bytes <= bounds.mem_ceiling_bytes:
        raise ValueError(
            f"memory limit {mem_bytes} bytes is outside {bounds.mem_floor_bytes // _MIB}M..{bounds.mem_ceiling_bytes // _GIB}G "
            "(the hard ceiling)"
        )
    if not bounds.cpu_floor_pct <= cpu_pct <= bounds.cpu_ceiling_pct:
        raise ValueError(
            f"CPU quota {cpu_pct}% is outside {bounds.cpu_floor_pct}..{bounds.cpu_ceiling_pct}%"
        )
    return ModelGuardLimits(
        runtime_s=runtime_s,
        mem_bytes=mem_bytes,
        cpu_pct=cpu_pct,
        tasks_max=bounds.tasks_max,
        kind=kind,
    )


class HandlerRemoteLaneGuardLimits:
    def handle(
        self, request: ModelRemoteLaneGuardLimitsRequest
    ) -> ModelRemoteLaneGuardLimitsVerdict:
        bounds = request.bounds
        runtime = int(request.timeout_s) + bounds.grace_s
        try:
            if runtime > bounds.hard_cap_s:
                longest = (bounds.hard_cap_s - bounds.grace_s) // 60
                raise ValueError(
                    f"a timeout of {int(request.timeout_s)}s plus the {bounds.grace_s}s grace passes the "
                    f"{bounds.hard_cap_s}s ({bounds.hard_cap_s // 3600} h) hard cap; the longest allowed timeout is {longest} minutes"
                )
            mem = (
                parse_size(request.mem_max)
                if request.mem_max
                else bounds.default_mem_bytes
            )
            cpu = (
                int(request.cpu_quota) if request.cpu_quota else bounds.default_cpu_pct
            )
            limits = _limits(runtime, mem, cpu, request.kind, bounds)
        except ValueError as exc:
            return ModelRemoteLaneGuardLimitsVerdict(limits=None, refusal=str(exc))
        return ModelRemoteLaneGuardLimitsVerdict(limits=limits, refusal=None)
