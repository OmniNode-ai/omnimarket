# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read each rolled runner pool's canary result, by the pools the contract declares (OMN-22945)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from omnimarket.nodes.node_runner_pool_canary_compute.models.model_runner_pool_canary import (
    EnumRunnerPoolCanaryState,
    ModelRunnerPoolCanaryRequest,
    ModelRunnerPoolCanaryVerdict,
    ModelRunnerPoolObservation,
    ModelRunnerPoolRule,
    ModelRunnerPoolVerdict,
)

_CONTRACT = Path(__file__).parents[1] / "contract.yaml"
_MARKER = re.compile(
    r"^CANARY_RESULT pool=(?P<pool>\S+) runner=(?P<runner>\S+) exit=(?P<exit>-?\d+)\s*$",
    re.MULTILINE,
)


def load_pool_rules(contract: Path = _CONTRACT) -> tuple[ModelRunnerPoolRule, ...]:
    """Read the pool_rules of a contract; a missing or malformed list is refused."""
    data = yaml.safe_load(contract.read_text(encoding="utf-8"))
    rules = data.get("pool_rules") if isinstance(data, dict) else None
    if not isinstance(rules, list) or not rules:
        raise ValueError("contract pool_rules must be a non-empty list")
    return tuple(ModelRunnerPoolRule(**rule) for rule in rules)


def parse_canary_log(log: str) -> ModelRunnerPoolObservation:
    """Read the canary workflow's last CANARY_RESULT line; a log without one has no exit status."""
    found = list(_MARKER.finditer(log))
    if not found:
        return ModelRunnerPoolObservation()
    m = found[-1]
    code = int(m.group("exit"))
    return ModelRunnerPoolObservation(
        pool=m.group("pool"),
        job_conclusion="success" if code == 0 else "failure",
        runner_name=m.group("runner"),
        exit_code=code,
    )


def _runs_on_pool(runner_name: str | None, rule: ModelRunnerPoolRule) -> bool:
    return bool(runner_name) and any(
        re.fullmatch(re.escape(prefix) + r"-\d+", runner_name or "")
        for prefix in rule.runner_name_prefixes
    )


class HandlerRunnerPoolCanary:
    """Pure compute over canary observations and the contract's pool_rules."""

    def __init__(self, rules: tuple[ModelRunnerPoolRule, ...] | None = None) -> None:
        self._rules = rules if rules is not None else load_pool_rules()

    def handle(
        self, request: ModelRunnerPoolCanaryRequest
    ) -> ModelRunnerPoolCanaryVerdict:
        """Return WORKING or NOT_WORKING with a reason for every declared pool."""
        verdicts = tuple(self._judge(rule, request) for rule in self._rules)
        return ModelRunnerPoolCanaryVerdict(
            correlation_id=request.correlation_id,
            verdicts=verdicts,
            all_working=all(
                v.state is EnumRunnerPoolCanaryState.WORKING for v in verdicts
            ),
        )

    def _judge(
        self, rule: ModelRunnerPoolRule, request: ModelRunnerPoolCanaryRequest
    ) -> ModelRunnerPoolVerdict:
        bad = EnumRunnerPoolCanaryState.NOT_WORKING
        own = [o for o in request.observations if _runs_on_pool(o.runner_name, rule)]
        if not own:
            return ModelRunnerPoolVerdict(
                pool=rule.pool,
                state=bad,
                reason="canary missing: no job ran on a runner of this pool",
            )
        good = [o for o in own if o.exit_code == 0 and o.job_conclusion == "success"]
        if not good:
            seen = ",".join(
                f"{o.runner_name}:exit={o.exit_code}:{o.job_conclusion}" for o in own
            )
            return ModelRunnerPoolVerdict(
                pool=rule.pool, state=bad, reason=f"canary not exit 0 ({seen})"
            )
        registered = request.registered_by_pool.get(rule.pool, 0)
        online = request.online_by_pool.get(rule.pool, 0)
        if registered <= 0:
            return ModelRunnerPoolVerdict(
                pool=rule.pool,
                state=bad,
                reason="no runner row for this pool in the organization listing",
            )
        if online < registered:
            return ModelRunnerPoolVerdict(
                pool=rule.pool,
                state=bad,
                reason=f"runners not all online ({online} of {registered})",
            )
        return ModelRunnerPoolVerdict(
            pool=rule.pool,
            state=EnumRunnerPoolCanaryState.WORKING,
            reason=f"canary exit 0 on {good[0].runner_name}; {online} of {registered} runners online",
        )


__all__ = ["HandlerRunnerPoolCanary", "load_pool_rules", "parse_canary_log"]
