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
- A Codex lane holds no Claude login, so no Claude mark keeps it off a host. A
  Codex-specific usage mark does keep it off that host. It takes
  the admitted host the Codex bar passes with the most free capacity, then memory,
  then the name.

Dispatch-venv drift (OMN-20862): when the launching host's dispatch-venv lock hash and
a host's differ, the host takes no lane. A lane that finds no host only because of that
is answered VENV_DRIFT, naming both hashes with one reconcile intent for the drifted
hosts, never a bare no-host. Drift counts only on a host that would otherwise take the
lane (a usage mark, the admission bar, the pin or a full host is a different reason). A
hash that was not supplied on a drift-judged request is VENV_UNKNOWN, never a pass. A
request carrying no hash and not marked ``dispatch_venv_required`` is an old producer's
and places as before.

OMN-21396: supplied ladder facts choose the cheapest eligible engine up to the route
ceiling, escalating past failed attempts and returning one experiment row for the last.
A lane declaring a capability the Codex sandbox cannot grant skips the Codex rung.
RULING 2026-10-28T13:32:09Z makes Sonnet the default and admits Opus only with an
escalation_reason (complexity:<B4 or above> or sonnet-failed:<run or lane>), for legacy
requests and pinned routes alike; a failed Sonnet attempt is itself that reason.
"""

from __future__ import annotations

from omnimarket.handlers.handler_engine_ladder import load_engine_ladder
from omnimarket.models.model_engine_experiment import (
    CLAUDE_RUNGS,
    CODEX_UNGRANTABLE_CAPABILITIES,
    ESCALATING_OUTCOMES,
    LOW_RISK_CLASSES,
    NO_ENGINE,
    QUOTA_FREE_RUNGS,
    RUNG_GATES,
    ModelEngineExperimentRow,
)
from omnimarket.models.model_engine_ladder import ModelEngineLadder
from omnimarket.nodes.node_lane_model_policy_compute.handlers.handler_lane_model_policy import (
    lane_model_refusal,
)

from ..models import (
    CODEX_ENGINE,
    EnumRemoteLaneOutcome,
    ModelRemoteLaneHostReading,
    ModelRemoteLaneHostVenv,
    ModelRemoteLaneHostVerdict,
    ModelRemoteLaneLadder,
    ModelRemoteLanePlacementRequest,
    ModelRemoteLanePlacementResult,
    ModelRemoteLaneReconcileIntent,
    ModelRemoteLaneRungVerdict,
    ModelRemoteLaneVenvDrift,
)

_VENV_DRIFT = "venv-drift"
_VENV_UNKNOWN = "venv-unknown"


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


def _drift_judged(request: ModelRemoteLanePlacementRequest) -> bool:
    return (
        request.dispatch_venv_required
        or request.launching_venv_hash is not None
        or any(r.dispatch_venv_hash is not None for r in request.readings)
    )


def _venv_reason(
    request: ModelRemoteLanePlacementRequest, reading: ModelRemoteLaneHostReading
) -> str | None:
    """The reason the venv keeps this host off, else None (equal hashes)."""
    if request.launching_venv_hash is None or reading.dispatch_venv_hash is None:
        return _VENV_UNKNOWN
    if reading.dispatch_venv_hash != request.launching_venv_hash:
        return _VENV_DRIFT
    return None


def _select(
    request: ModelRemoteLanePlacementRequest, blocked: dict[str, str]
) -> tuple[ModelRemoteLaneHostReading | None, dict[str, str]]:
    """The pick and every host's reason for not being it, with ``blocked`` hosts kept off."""
    codex = request.engine == CODEX_ENGINE
    limited = set(request.codex_limited_hosts if codex else request.limited_hosts)
    expired = set() if codex else set(request.auth_expired_hosts)
    reasons: dict[str, str] = {}
    admitted: list[ModelRemoteLaneHostReading] = []
    for reading in request.readings:
        if reading.name in limited:
            reasons[reading.name] = "usage-limited"
        elif reading.name in expired:
            reasons[reading.name] = "auth-expired"
        elif reading.name in blocked:
            reasons[reading.name] = blocked[reading.name]
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
    return pick, reasons


def _escalation_reason(ladder: ModelRemoteLaneLadder) -> str | None:
    """The declared reason, else the lane's own failed Sonnet attempt."""
    if ladder.escalation_reason:
        return ladder.escalation_reason
    if any(
        a.engine == "sonnet" and a.outcome in ESCALATING_OUTCOMES
        for a in ladder.attempts
    ):
        return f"sonnet-failed:{ladder.lane}"
    return None


def _rung_reason(
    engines: ModelEngineLadder, ladder: ModelRemoteLaneLadder, rung: str
) -> str | None:
    """The first route or readiness rule that refuses a rung."""
    if engines.index(rung) > engines.index(ladder.ceiling):
        return "above-route-ceiling"
    if refusal := lane_model_refusal(
        rung, escalation_reason=_escalation_reason(ladder)
    ):
        return refusal
    if ladder.pinned and rung != ladder.ceiling:
        return "pinned-to-route"
    if ladder.claude_required and rung not in CLAUDE_RUNGS:
        return "claude-required"
    failing = {
        "landing-worker": ladder.landing_worker,
        # A lane that declares no risk class is judged by its kind (a small-fix lane).
        "risk-class": (ladder.risk_class or ladder.lane_kind) not in LOW_RISK_CLASSES,
        "no-writable-or-check": not ladder.code_edit_ready,
        "no-local-slot": ladder.local_model_slots == 0,
        "sandbox-capability": bool(
            set(ladder.required_capabilities) & set(CODEX_UNGRANTABLE_CAPABILITIES)
        ),
    }
    return next((gate for gate in RUNG_GATES[rung] if failing[gate]), None)


def _experiment(
    ladder: ModelRemoteLaneLadder, rung: str | None
) -> ModelEngineExperimentRow | None:
    """Describe the last finished attempt and the placement's next engine."""
    if not ladder.attempts:
        return None
    last = ladder.attempts[-1]
    return ModelEngineExperimentRow(
        lane=ladder.lane,
        ticket=ladder.ticket,
        attempt=len(ladder.attempts),
        engine=last.engine,
        model=last.model,
        outcome=last.outcome,
        failure_cause=last.failure_cause,
        duration_s=last.duration_s,
        next_engine=rung or NO_ENGINE,
    )


class HandlerRemoteLanePlacement:
    """Choose the host for one remote lane from the pool's evaluated readings.

    The engine ladder is the ``config.engine_ladder`` of this node's contract, read once at
    composition (OMN-21475); a caller may inject a validated ladder.
    """

    def __init__(self, engines: ModelEngineLadder | None = None) -> None:
        self._engines = load_engine_ladder() if engines is None else engines

    def handle(
        self, request: ModelRemoteLanePlacementRequest
    ) -> ModelRemoteLanePlacementResult:
        ladder = request.ladder
        if ladder is None:
            return self._place(request)
        last_result = ModelRemoteLanePlacementResult(host=None, engine=request.engine)
        start = 0
        if ladder.attempts:
            last = ladder.attempts[-1]
            if last.outcome not in ESCALATING_OUTCOMES:
                return last_result.model_copy(
                    update={"experiment": _experiment(ladder, None)}
                )
            start = self._engines.index(last.engine) + 1
        rungs: list[ModelRemoteLaneRungVerdict] = []
        for rung in self._engines.rungs[start:]:
            reason = _rung_reason(self._engines, ladder, rung)
            if reason is not None:
                rungs.append(ModelRemoteLaneRungVerdict(engine=rung, reason=reason))
                continue
            placement_request = request.model_copy(
                update={
                    "engine": self._engines.host_engine(rung),
                    "ladder": None,
                    "escalation_reason": _escalation_reason(ladder),
                    **(
                        {"limited_hosts": (), "auth_expired_hosts": ()}
                        if rung in QUOTA_FREE_RUNGS
                        else {}
                    ),
                }
            )
            result = self._place(placement_request)
            if result.outcome == EnumRemoteLaneOutcome.ADMITTED:
                rungs.append(ModelRemoteLaneRungVerdict(engine=rung, reason="chosen"))
                return result.model_copy(
                    update={
                        "rung": rung,
                        "engine": self._engines.host_engine(rung),
                        "rungs": tuple(rungs),
                        "experiment": _experiment(ladder, rung),
                    }
                )
            rungs.append(
                ModelRemoteLaneRungVerdict(
                    engine=rung, reason=f"no-capacity: {result.outcome.value}"
                )
            )
            last_result = result
        return last_result.model_copy(
            update={
                "host": None,
                "rung": None,
                "engine": request.engine,
                "rungs": tuple(rungs),
                "experiment": _experiment(ladder, None),
            }
        )

    def _place(
        self, request: ModelRemoteLanePlacementRequest
    ) -> ModelRemoteLanePlacementResult:
        if refusal := lane_model_refusal(
            request.engine, escalation_reason=request.escalation_reason
        ):
            return ModelRemoteLanePlacementResult(
                host=None,
                engine=request.engine,
                verdicts=tuple(
                    ModelRemoteLaneHostVerdict(host=reading.name, reason=refusal)
                    for reading in request.readings
                ),
            )
        pick, reasons = _select(request, {})
        blocked: dict[str, str] = {}
        if _drift_judged(request):
            # Only a host that would take the lane absent the venv can be blamed on it.
            for reading in request.readings:
                if reasons.get(reading.name, "ranked-lower") in (
                    "ranked-lower",
                    "lab-host-first",
                ):
                    reason = _venv_reason(request, reading)
                    if reason is not None:
                        blocked[reading.name] = reason
        venv_drift: ModelRemoteLaneVenvDrift | None = None
        reconcile: ModelRemoteLaneReconcileIntent | None = None
        outcome = EnumRemoteLaneOutcome.NO_HOST
        if blocked:
            unblocked_pick, reasons = _select(request, blocked)
            if unblocked_pick is not None:
                pick = unblocked_pick
            else:
                held_off = [r for r in request.readings if r.name in blocked]
                venv_drift = ModelRemoteLaneVenvDrift(
                    launching_hash=request.launching_venv_hash,
                    hosts=tuple(
                        ModelRemoteLaneHostVenv(host=r.name, hash=r.dispatch_venv_hash)
                        for r in held_off
                    ),
                )
                drifted = tuple(
                    r.name for r in held_off if blocked[r.name] == _VENV_DRIFT
                )
                if drifted:
                    outcome = EnumRemoteLaneOutcome.VENV_DRIFT
                    reconcile = ModelRemoteLaneReconcileIntent(
                        hosts=drifted, target_hash=request.launching_venv_hash
                    )
                else:
                    outcome = EnumRemoteLaneOutcome.VENV_UNKNOWN
                pick = None
        if pick is not None:
            outcome = EnumRemoteLaneOutcome.ADMITTED

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
            outcome=outcome,
            venv_drift=venv_drift,
            reconcile=reconcile,
        )
