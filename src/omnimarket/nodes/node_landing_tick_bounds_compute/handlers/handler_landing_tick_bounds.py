# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing controller tick's duration bounds (OMN-17427).

On 2026-10-19 the controller's last 12 chained ticks ran 323 to 694 s, so it acted 3 to 5 times an hour, and the
receipt's timing named the phases but nothing compared them with what a tick should take. The budget (a tick refuses
before it acts) and the deadline (a running tick ends itself) bound the worst tick; the target is what a tick should
take, and a tick past it says which three leaf phases spent the time. ``read_s`` and ``perform_s`` are sums of other
phases, so they are never ranked.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.nodes.node_landing_tick_bounds_compute.models.model_landing_tick_bounds import (
    ModelLandingTickTarget,
    ModelLandingTickTargetRequest,
)

TICK_BUDGET_S = 720.0
BUDGET_ENV = "LANDING_TICK_BUDGET_S"
# A tick still running this long after its start ends itself, with an UNKNOWN receipt naming the deadline, so launchd's
# next start runs a fresh tick instead of nothing: the drain watchdog's kickstart reads a receipt only 20 minutes stale
# and loops_check's running-tick bound is 30. The slowest recorded tick ran 926 s (tick 2853). 0 turns it off.
TICK_DEADLINE_S = 1500.0
DEADLINE_ENV = "LANDING_TICK_DEADLINE_S"
TICK_TARGET_S = 120.0
TARGET_ENV = "LANDING_TICK_TARGET_S"
PHASE_SUMS = frozenset({"total_s", "read_s", "perform_s"})
TOP_PHASES = 3


def budget_s(env: Mapping[str, str]) -> float:
    """The tick budget; a malformed override raises, as it always has."""
    override = env.get(BUDGET_ENV)
    return float(override) if override is not None else TICK_BUDGET_S


def _float_or(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        return float(env[name]) if env.get(name) is not None else default
    except ValueError:
        return default


def deadline_s(env: Mapping[str, str]) -> float:
    return _float_or(env, DEADLINE_ENV, TICK_DEADLINE_S)


def target_s(env: Mapping[str, str]) -> float:
    return _float_or(env, TARGET_ENV, TICK_TARGET_S)


class HandlerLandingTickBounds:
    def handle(self, request: ModelLandingTickTargetRequest) -> ModelLandingTickTarget:
        phases = sorted(
            (
                (name, float(s))
                for name, s in request.timing.items()
                if name.endswith("_s")
                and name not in PHASE_SUMS
                and isinstance(s, (int, float))
                and not isinstance(s, bool)
            ),
            key=lambda kv: (-kv[1], kv[0]),
        )
        total = request.timing.get("total_s")
        total_s = float(total) if isinstance(total, (int, float)) else 0.0
        return ModelLandingTickTarget(
            target_s=float(request.target_s),
            total_s=total_s,
            over=total_s > request.target_s,
            top=[[name, s] for name, s in phases[:TOP_PHASES]],
        )


def judge(
    timing: Mapping[str, object], env: Mapping[str, str]
) -> ModelLandingTickTarget:
    """The tick's verdict against the target its environment names."""
    return HandlerLandingTickBounds().handle(
        ModelLandingTickTargetRequest(timing=timing, target_s=target_s(env))
    )
