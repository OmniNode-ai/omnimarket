# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which pool host takes one remote lane (OMN-20669).

A port of the remote-lane runner's ``eligible``, ``choose_codex`` and the shared
``choose_lane`` over readings the placement reader already evaluated:

- A Claude-engine lane never lands on a host with a live usage-limit or auth-expired
  mark, nor on one the lane admission bar refuses, and only on the pinned host when
  one is pinned. Of the hosts holding a login for the engine with a lane slot, the
  lab hosts come first and the launching host only when no lab host has a slot; the
  pick is the best spread key (an idle host, then the lowest fill, then free capacity,
  memory, the launching host, the name).
- A Codex lane holds no Claude login, so no Claude mark keeps it off a host. It takes
  the admitted host the Codex bar passes with the most free capacity, then memory,
  then the name.
"""

from __future__ import annotations

from ..models import (
    CODEX_ENGINE,
    ModelRemoteLaneHostReading,
    ModelRemoteLaneHostVerdict,
    ModelRemoteLanePlacementRequest,
    ModelRemoteLanePlacementResult,
)


def _rank(reading: ModelRemoteLaneHostReading) -> float:
    return float("-inf") if reading.rank_free is None else reading.rank_free


def _spread_key(
    reading: ModelRemoteLaneHostReading,
) -> tuple[bool, float, float, float, bool, str]:
    cap = reading.lane_cap
    fill = 1.0 if cap <= 0 else min(1.0, reading.placed / cap)
    return (
        reading.placed == 0,
        -fill,
        _rank(reading),
        reading.mem_avail_gb,
        reading.local,
        reading.name,
    )


def _codex_key(reading: ModelRemoteLaneHostReading) -> tuple[float, float, str]:
    return (_rank(reading), reading.mem_avail_gb, reading.name)


class HandlerRemoteLanePlacement:
    """Choose the host for one remote lane from the pool's evaluated readings."""

    def handle(
        self, request: ModelRemoteLanePlacementRequest
    ) -> ModelRemoteLanePlacementResult:
        codex = request.engine == CODEX_ENGINE
        limited = set() if codex else set(request.limited_hosts)
        expired = set() if codex else set(request.auth_expired_hosts)
        reasons: dict[str, str] = {}
        admitted: list[ModelRemoteLaneHostReading] = []
        for reading in request.readings:
            if reading.name in limited:
                reasons[reading.name] = "usage-limited"
            elif reading.name in expired:
                reasons[reading.name] = "auth-expired"
            elif reading.lane_admission_refusal is not None:
                reasons[reading.name] = f"admission: {reading.lane_admission_refusal}"
            elif request.pinned_host and reading.name != request.pinned_host:
                reasons[reading.name] = "not-pinned"
            else:
                admitted.append(reading)

        pick: ModelRemoteLaneHostReading | None = None
        if codex:
            fit = []
            for reading in admitted:
                if reading.codex_refusal is None:
                    fit.append(reading)
                else:
                    reasons[reading.name] = f"codex: {reading.codex_refusal}"
            if fit:
                pick = max(fit, key=_codex_key)
        else:
            held = []
            for reading in admitted:
                if request.engine in reading.engines:
                    held.append(reading)
                else:
                    reasons[reading.name] = f"no-{request.engine}-login"
            for tier in (
                [r for r in held if not r.local],
                [r for r in held if r.local],
            ):
                fit = []
                for reading in tier:
                    if reading.lane_slots > 0:
                        fit.append(reading)
                    else:
                        reasons[reading.name] = "no-lane-slot"
                if fit:
                    pick = max(fit, key=_spread_key)
                    break
            if pick is not None and not pick.local:
                for reading in held:
                    if reading.local and reading.name not in reasons:
                        reasons[reading.name] = "lab-host-first"

        verdicts = []
        for reading in request.readings:
            if pick is not None and reading.name == pick.name:
                reason = "chosen"
            else:
                reason = reasons.get(reading.name, "ranked-lower")
            verdicts.append(
                ModelRemoteLaneHostVerdict(host=reading.name, reason=reason)
            )
        return ModelRemoteLanePlacementResult(
            host=pick.name if pick is not None else None,
            engine=request.engine,
            local=pick.local if pick is not None else False,
            verdicts=tuple(verdicts),
        )
