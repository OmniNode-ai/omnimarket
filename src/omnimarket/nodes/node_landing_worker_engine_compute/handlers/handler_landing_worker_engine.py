# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing worker's engine choice at placement (OMN-17427, OMN-20531, OMN-20106).

Codex first for a code or mechanical class on the first rung (``landing_facts.codex_first``), the node's Claude engine
otherwise, and then the reverse fallback: a worker no Claude host can take now runs on the Codex engine where a codex
host has room, instead of waiting out its lease.

Measured 2026-10-21: four lab hosts were Claude-limited until 2026-10-25T23:00Z while their Codex logins
answered with free slots, and the tick's pool capacity counted those slots (node_landing_pool_capacity_compute). Only a
first-rung code class could use them, so every escalation and head-cap brief waited out its lease at ``host: null`` and
ended lab_capacity_unavailable. Claude stays first wherever it has headroom: the fallback runs only after ``choose``
found none. A cause brief keeps waiting for Claude. ``LANDING_CODEX_MECHANICAL=0`` turns off every Codex route and
``LANDING_CODEX_FALLBACK=0`` only this fallback.
"""

from __future__ import annotations

from collections.abc import Callable

from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)

from omnimarket.handlers.handler_engine_ladder import load_engine_ladder
from omnimarket.handlers.rules_stop_gate import (
    engine_stop_scopes,
    gate_dispatch,
    load_andon_stop_contract,
)
from omnimarket.models.model_engine_ladder import ModelEngineLadder
from omnimarket.models.stop_gate import EnumStopGateOutcome
from omnimarket.nodes.node_landing_worker_engine_compute.models.model_landing_worker_engine import (
    ModelLandingWorkerEngineRequest,
    ModelLandingWorkerEngineResult,
)

CODEX_MECHANICAL_ENV = "LANDING_CODEX_MECHANICAL"
CODEX_FALLBACK_ENV = "LANDING_CODEX_FALLBACK"

# The code rule a Codex worker placed by the fallback reads in place of the first-rung rules (landing_worker_run.render_prompt).
CODEX_FALLBACK_RULE = """Code: you are the Codex engine of this worker (OMN-17427). The node sent this brief to {engine}, but no pool
host had Claude headroom (every Claude host was usage-limited or full), so it runs on Codex instead of waiting. Do the brief's work
yourself in this worktree, "{worktree}": write any fix yourself, run the focused tests and own the commit. Do not run delegation_step.py
code and do not start a second Codex process. Never run with the sandbox off. Do not push a fix you could not make pass: when you cannot
finish, write blocked with reason=codex_attempt_failed and say what you tried and what still fails, then exit. If a write is refused
("Failed to write file", a "bwrap:" error), write blocked with reason=codex_sandbox_refusal, then exit."""


def codex_fallback_allowed(request: ModelLandingWorkerEngineRequest) -> bool:
    """Whether a worker no Claude host can take may run on Codex: not a cause brief, not one Codex was already tried first
    for, and neither switch off."""
    if request.cause or request.codex_first:
        return False
    return all(
        request.env.get(name, "1").strip() != "0"
        for name in (CODEX_MECHANICAL_ENV, CODEX_FALLBACK_ENV)
    )


class HandlerLandingWorkerEngine:
    """The engine choice; the engines a stop or the caller keeps off are named by the request's facts and the
    ladder contract's ``config.engine_ladder``, never by this module."""

    def __init__(
        self,
        engines: ModelEngineLadder | None = None,
        stop_fsm: ModelFSMSubcontract | None = None,
    ) -> None:
        self._engines = engines
        self._stop_fsm = stop_fsm

    def _refusal(
        self, request: ModelLandingWorkerEngineRequest, engine: str
    ) -> tuple[str | None, str | None]:
        """(why the engine is kept off, the stop line to record when a stop names it and admits)."""
        if engine in request.disallowed_engines:
            return f"disallowed: {engine}", None
        facts = request.stop_gate
        if facts is None:
            return None, None
        if self._engines is None:
            self._engines = load_engine_ladder()
        if self._stop_fsm is None:
            self._stop_fsm = load_andon_stop_contract()[0]
        gate = gate_dispatch(
            facts, engine_stop_scopes(engine, self._engines), self._stop_fsm
        )
        if gate.outcome is EnumStopGateOutcome.REFUSE:
            return gate.line, None
        return None, gate.line if gate.stop_id is not None else None

    def handle(
        self, request: ModelLandingWorkerEngineRequest
    ) -> ModelLandingWorkerEngineResult:
        lp, readings, allowed = (
            request.placement,
            list(request.readings),
            request.codex_allowed,
        )
        record: dict[str, object] = {}
        gated = bool(request.disallowed_engines) or request.stop_gate is not None
        refused: dict[str, str] = {}
        observed_stops: dict[str, str] = {}

        def kept_off(engine_of: Callable[[], str]) -> bool:
            if not gated:
                return False
            engine = engine_of()
            why, observed = self._refusal(request, engine)
            if why is not None:
                refused[engine] = why
                return True
            if observed is not None:
                observed_stops[engine] = observed
            return False

        pick = None
        if request.codex_first:
            if not kept_off(lambda: lp.CODEX_ENGINE):
                pick = lp.choose_codex(readings, allowed)
            record["codex_first"] = True
            if pick is None:
                record["codex_fallback"] = lp.preflight_skips(
                    readings, lp.CODEX_ENGINE, allowed
                )
        if pick is None and not kept_off(
            lambda: lp.resolve_engine(request.requested_engine)
        ):
            pick = lp.choose(request.requested_engine, readings)
        if (
            pick is None
            and codex_fallback_allowed(request)
            and not kept_off(lambda: lp.CODEX_ENGINE)
        ):
            pick = lp.choose_codex(readings, allowed)
            if pick is not None:
                record["claude_no_headroom"] = lp.preflight_skips(
                    readings, lp.resolve_engine(request.requested_engine)
                )
        if refused:
            record["engine_refused"] = refused
        if observed_stops:
            record["stop_observed"] = observed_stops
        return ModelLandingWorkerEngineResult(pick=pick, record=record)
