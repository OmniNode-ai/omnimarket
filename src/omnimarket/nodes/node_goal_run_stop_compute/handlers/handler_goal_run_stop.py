# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide whether a goal-driven run continues, is retried or stops, and how the stop is analysed.

Pure: the caller reads the experiment's caps file and passes its text, the counters and the
signals. The first stop reached wins and is recorded with the counters at that instant. An
infra fault is retried under the same run key and only then excluded; it is never scored as
giving up (OMN-21974).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from omnimarket.models.model_goal_run import (
    EnumGoalAnalysisRole,
    EnumGoalRunAction,
    EnumGoalStopReason,
    ModelGoalCaps,
    ModelGoalCounters,
)
from omnimarket.nodes.node_goal_run_stop_compute.models.model_goal_run_stop import (
    ModelGoalRunAnalysis,
    ModelGoalRunStopDecision,
    ModelGoalRunStopRequest,
    ModelGoalRunStopRules,
)

CONTRACT_PATH = Path(__file__).parents[1] / "contract.yaml"

_CAP_REACHED = (
    (EnumGoalStopReason.TOKEN_CAP, "tokens", "token_cap"),
    (EnumGoalStopReason.WALL_CLOCK_CAP, "wall_clock_s", "wall_clock_cap_s"),
    (EnumGoalStopReason.TURN_CAP, "turns", "turn_cap"),
    (EnumGoalStopReason.COMPACTION_CAP, "compactions", "compaction_cap"),
)


def load_rules(contract: Path = CONTRACT_PATH) -> ModelGoalRunStopRules:
    """Read the stop_rules of a contract; a missing or unknown rule is refused."""
    data = yaml.safe_load(contract.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("stop_rules"), dict):
        raise ValueError("contract stop_rules must be a mapping")
    return ModelGoalRunStopRules.model_validate(data["stop_rules"])


def parse_caps(caps_file_text: str) -> tuple[ModelGoalCaps, str]:
    """The caps in the experiment's data file and the sha256 of the file's bytes."""
    loaded = yaml.safe_load(caps_file_text)
    if not isinstance(loaded, dict):
        raise ValueError("the caps file must be a mapping of the five caps")
    return (
        ModelGoalCaps.model_validate(loaded),
        hashlib.sha256(caps_file_text.encode("utf-8")).hexdigest(),
    )


def run_key(item_id: str, predicate_sha256: str, caps_file_sha256: str) -> str:
    """The key of a run: the item, the goal's hash and the caps file's hash."""
    joined = "\n".join((item_id, predicate_sha256, caps_file_sha256))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


class HandlerGoalRunStop:
    """Pure compute over the caller-read counters, signals and caps file."""

    def __init__(self, rules: ModelGoalRunStopRules | None = None) -> None:
        self._rules = rules if rules is not None else load_rules()

    def handle(self, request: ModelGoalRunStopRequest) -> ModelGoalRunStopDecision:
        caps, caps_sha = parse_caps(request.caps_file_text)
        key = run_key(request.item_id, request.goal.predicate_sha256, caps_sha)
        base = {
            "correlation_id": request.correlation_id,
            "run_key": key,
            "caps_file_sha256": caps_sha,
            "caps": caps,
        }
        reached = self._reached(request, caps)
        if request.prior_stop is not None:
            # The first stop reached wins; a later reason changes nothing.
            return self._stopped(
                request,
                caps,
                base,
                request.prior_stop.stop_reason,
                request.prior_stop.counters_at_stop,
                tuple(dict.fromkeys((request.prior_stop.stop_reason, *reached))),
                request.prior_stop.detail,
            )
        if not reached:
            return ModelGoalRunStopDecision(
                **base,
                action=EnumGoalRunAction.CONTINUE,
                counters_at_stop=request.counters,
            )
        winner = reached[0]
        if (
            winner is EnumGoalStopReason.INFRA_FAULT
            and request.infra_fault_retries_used < self._rules.infra_fault_retry_limit
        ):
            return ModelGoalRunStopDecision(
                **base,
                action=EnumGoalRunAction.RETRY,
                retry_reason=EnumGoalStopReason.INFRA_FAULT,
                reached_reasons=reached,
                counters_at_stop=request.counters,
                detail=request.signals.detail,
            )
        return self._stopped(
            request,
            caps,
            base,
            winner,
            request.counters,
            reached,
            request.signals.detail,
        )

    def _reached(
        self, request: ModelGoalRunStopRequest, caps: ModelGoalCaps
    ) -> tuple[EnumGoalStopReason, ...]:
        """Every reason reached at this instant, in the contract's precedence order."""
        found: set[EnumGoalStopReason] = set()
        sig = request.signals
        for flag, reason in (
            (sig.goal_met, EnumGoalStopReason.GOAL_MET),
            (sig.agent_blocked, EnumGoalStopReason.AGENT_BLOCKED),
            (sig.operator_stop, EnumGoalStopReason.OPERATOR_STOP),
            (sig.refused_premise, EnumGoalStopReason.REFUSED_PREMISE),
            (sig.infra_fault, EnumGoalStopReason.INFRA_FAULT),
        ):
            if flag:
                found.add(reason)
        counters = request.counters
        for reason, counter, cap in _CAP_REACHED:
            if getattr(counters, counter) >= getattr(caps, cap):
                found.add(reason)
        if counters.turns_without_subcheck_change >= caps.no_progress_window_turns:
            found.add(EnumGoalStopReason.NO_PROGRESS)
        if request.loop_status is not None:
            mapped = self._map_loop(
                request.loop_status.loop, request.loop_status.status
            )
            if mapped is not None:
                found.add(mapped)
        return tuple(r for r in self._rules.precedence if r in found)

    def _map_loop(self, loop: str, status: str) -> EnumGoalStopReason | None:
        states = self._rules.loop_status_map.get(loop)
        if states is None:
            raise ValueError(f"no stop-state map for loop {loop!r}")
        if status not in states:
            raise ValueError(f"loop {loop!r} state {status!r} is not mapped")
        return states[status]

    def _stopped(
        self,
        request: ModelGoalRunStopRequest,
        caps: ModelGoalCaps,
        base: dict[str, object],
        reason: EnumGoalStopReason,
        at_stop: ModelGoalCounters,
        reached: tuple[EnumGoalStopReason, ...],
        detail: str,
    ) -> ModelGoalRunStopDecision:
        return ModelGoalRunStopDecision.model_validate(
            {
                **base,
                "action": EnumGoalRunAction.STOP,
                "stop_reason": reason,
                "reached_reasons": reached,
                "counters_at_stop": at_stop,
                "detail": detail,
                "analysis": self._analysis(request, caps, reason, at_stop, detail),
            }
        )

    def _analysis(
        self,
        request: ModelGoalRunStopRequest,
        caps: ModelGoalCaps,
        reason: EnumGoalStopReason,
        at_stop: ModelGoalCounters,
        detail: str,
    ) -> ModelGoalRunAnalysis:
        rule = self._rules.stop_roles[reason]
        censored = rule.role is EnumGoalAnalysisRole.RIGHT_CENSORED
        excluded = rule.role is EnumGoalAnalysisRole.EXCLUDED
        censored_at: ModelGoalCounters | None = None
        if censored:
            # Censored AT the cap: the capped dimension carries the cap, not the counter.
            update = {
                counter: getattr(caps, cap)
                for cap_reason, counter, cap in _CAP_REACHED
                if cap_reason is reason
            }
            censored_at = at_stop.model_copy(update=update)
        reason_text = rule.exclusion_reason
        if excluded and reason is EnumGoalStopReason.INFRA_FAULT:
            reason_text = (
                f"{reason_text} after {request.infra_fault_retries_used} retries"
            )
        if excluded and detail:
            reason_text = f"{reason_text}: {detail}"
        return ModelGoalRunAnalysis(
            role=rule.role,
            right_censored=censored,
            counts_as_success=rule.counts_as_success,
            in_denominator=not excluded,
            alternate_role=rule.alternate_role,
            censored_at=censored_at,
            exclusion_reason=reason_text,
            counted=excluded,
            scored_as_giving_up=rule.role is EnumGoalAnalysisRole.FAILURE,
        )
