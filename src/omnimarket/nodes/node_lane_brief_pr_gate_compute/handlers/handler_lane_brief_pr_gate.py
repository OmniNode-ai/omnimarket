# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Brief gate, PR-opening check: the ``Opens PR in`` line, then the shared WIP rule.

A brief opens a PR in the repository its column-zero ``Opens PR in:`` line names; ``none`` declares
that it opens none. A brief with no such line is reported under ``observe`` and refused under
``enforce``. A brief that names a repository is judged by the shared WIP rule
(``omnimarket.handlers.rules_wip_gate``), whose own enforcement decides refuse or warn. The mode and
the WIP policy a deployment injects into the constructor are never loosened by the request. Pure:
reads no file, takes ``now`` from the request.
"""

from __future__ import annotations

import re

from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)

from omnimarket.handlers.rules_wip_gate import (
    evaluate_wip_gate,
    load_wip_gate_contract,
)
from omnimarket.models.lane_brief.model_lane_brief_issue import ModelLaneBriefIssue
from omnimarket.models.lane_brief_pr_gate.enum_lane_brief_pr_gate_mode import (
    EnumLaneBriefPrGateMode,
)
from omnimarket.models.lane_brief_pr_gate.enum_lane_brief_pr_gate_verdict import (
    EnumLaneBriefPrGateVerdict,
)
from omnimarket.models.lane_brief_pr_gate.model_lane_brief_pr_gate_config import (
    ModelLaneBriefPrGateConfig,
)
from omnimarket.models.lane_brief_pr_gate.model_lane_brief_pr_gate_request import (
    ModelLaneBriefPrGateRequest,
)
from omnimarket.models.lane_brief_pr_gate.model_lane_brief_pr_gate_result import (
    ModelLaneBriefPrGateResult,
)
from omnimarket.models.wip_gate import (
    EnumWipOutcome,
    EnumWipReason,
    ModelWipGateConfig,
    ModelWipGateRequest,
    ModelWipPolicy,
)

_OPENS = "Opens PR in"
_NO_PR = "none"
_FENCE = re.compile(r"^(```|~~~)")
_OPENS_LINE = re.compile(rf"^{_OPENS}:[ \t]*(.*?)[ \t]*$")


def _opens_values(brief: str) -> list[str]:
    values: list[str] = []
    fenced = False
    for line in brief.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
        elif not fenced and (match := _OPENS_LINE.match(line)):
            values.append(match.group(1))
    return values


def _strictest(
    injected: ModelLaneBriefPrGateConfig | None,
    requested: ModelLaneBriefPrGateConfig | None,
) -> ModelLaneBriefPrGateConfig | None:
    """The injected configuration wins; a request may only make it stricter."""
    if injected is None:
        return requested
    if (
        requested is not None
        and requested.mode is EnumLaneBriefPrGateMode.ENFORCE
        and injected.mode is not EnumLaneBriefPrGateMode.ENFORCE
    ):
        return requested
    return injected


class HandlerLaneBriefPrGate:
    """``handle(ModelLaneBriefPrGateRequest) -> ModelLaneBriefPrGateResult``."""

    def __init__(
        self,
        *,
        config: ModelLaneBriefPrGateConfig | None = None,
        policy: ModelWipPolicy | None = None,
        fsm: ModelFSMSubcontract | None = None,
        wip_config: ModelWipGateConfig | None = None,
    ) -> None:
        self._config = config
        self._policy = policy
        loaded_fsm, loaded_config = load_wip_gate_contract()
        self._fsm = fsm or loaded_fsm
        self._wip_config = wip_config or loaded_config

    def handle(
        self, request: ModelLaneBriefPrGateRequest
    ) -> ModelLaneBriefPrGateResult:
        config = _strictest(self._config, request.config)
        policy = self._policy or request.policy
        values = _opens_values(request.brief)

        def refuse(
            issues: list[ModelLaneBriefIssue], text: str
        ) -> ModelLaneBriefPrGateResult:
            return ModelLaneBriefPrGateResult(
                verdict=EnumLaneBriefPrGateVerdict.REFUSED,
                mode=config.mode if config else None,
                issues=tuple(issues),
                line=f"REFUSE brief {text}",
                decided_at=request.now,
            )

        if config is None:
            return refuse(
                [
                    ModelLaneBriefIssue(
                        line="lane_brief_pr_gate.mode",
                        detail="no configuration reached the gate",
                    )
                ],
                "lane_brief_pr_gate.mode unread",
            )
        issues: list[ModelLaneBriefIssue] = []
        repo = ""
        if len(values) > 1:
            issues.append(
                ModelLaneBriefIssue(
                    line=_OPENS, detail=f"stated {len(values)} times: {values!r}"
                )
            )
        elif not values or not values[0]:
            issues.append(
                ModelLaneBriefIssue(
                    line=_OPENS,
                    detail="no such line" if not values else "empty",
                )
            )
        else:
            repo = values[0]
        enforce = config.mode is EnumLaneBriefPrGateMode.ENFORCE
        if issues:
            verdict = (
                EnumLaneBriefPrGateVerdict.REFUSED
                if enforce
                else EnumLaneBriefPrGateVerdict.REPORTED
            )
            word = "REFUSE" if enforce else "REPORT"
            return ModelLaneBriefPrGateResult(
                verdict=verdict,
                mode=config.mode,
                issues=tuple(issues),
                line=f"{word} brief {_OPENS}: {issues[0].detail} mode={config.mode.value}",
                decided_at=request.now,
            )
        if repo.lower() == _NO_PR:
            return ModelLaneBriefPrGateResult(
                verdict=EnumLaneBriefPrGateVerdict.ADMITTED,
                mode=config.mode,
                repo=repo,
                line=f"ADMIT brief opens no PR mode={config.mode.value}",
                decided_at=request.now,
            )
        if policy is None:
            return ModelLaneBriefPrGateResult(
                verdict=EnumLaneBriefPrGateVerdict.REFUSED,
                mode=config.mode,
                repo=repo,
                issues=(
                    ModelLaneBriefIssue(
                        line="wip_limits", detail="no WIP policy reached the gate"
                    ),
                ),
                line=f"REFUSE brief wip_limits unread repo={repo}",
                decided_at=request.now,
            )
        decision = evaluate_wip_gate(
            ModelWipGateRequest(
                repo=repo,
                dispatch_id=request.dispatch_id,
                now=request.now,
                state=request.state,
                policy=policy,
                config=self._wip_config,
                exemption=request.exemption,
                authorized_swarm_lanes=request.authorized_swarm_lanes,
                override=request.override,
                override_consumed_by=request.override_consumed_by,
            ),
            self._fsm,
        )
        if decision.outcome is EnumWipOutcome.REFUSE:
            verdict = EnumLaneBriefPrGateVerdict.REFUSED
        elif decision.outcome is EnumWipOutcome.WARN:
            verdict = EnumLaneBriefPrGateVerdict.REPORTED
        elif decision.reason is EnumWipReason.OVERRIDE:
            verdict = EnumLaneBriefPrGateVerdict.ADMITTED_OVERRIDE
        else:
            verdict = EnumLaneBriefPrGateVerdict.ADMITTED
        return ModelLaneBriefPrGateResult(
            verdict=verdict,
            mode=config.mode,
            repo=repo,
            wip=decision,
            line=f"{decision.line} mode={config.mode.value}",
            decided_at=request.now,
        )
