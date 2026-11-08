# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J8 (piece 8 to piece 9): the landing decision reads the lab-bound check run on the exact head (OMN-22919).

Canonical shape: ``handle(ModelJoinJ8Request) -> ModelJoinJ8Result``. The handler answers piece 9 on the floor's
emission contract, under the correlation id piece 8's chain (the check poster) carries:

* the head's lab-bound check is missing, from a superseded attempt or failing: a typed ``upstream_`` refusal that never
  started, and the decision names the same reason as the head's held reason;
* the check is still running inside its wait bound: nothing is emitted, the join is not judged yet;
* the current attempt's check passed on the exact head: a start, the landing decision, then a success when the decision
  merges the head, or a typed ``not_merged`` failure with the start before it when the decision holds it for another
  reason.

The decision is the node's own ``decide_landing``, called as the landing controller calls it. Everything else is
injected: the emission sink and the clock. Its settings are the contract's ``join_j8`` section.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from importlib import resources
from typing import Any, Literal

import yaml

from omnimarket.handlers.emission_sink import ProtocolEmissionSink
from omnimarket.models.floor_17a import ModelNodeEmission
from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_pr_landing_decision import (
    decide_landing,
)
from omnimarket.nodes.node_pr_landing_decision_compute.lab_check import (
    read_lab_check,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingActionKind,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_join_j8 import (
    EnumJoinJ8Outcome,
    ModelJoinJ8Policy,
    ModelJoinJ8Request,
    ModelJoinJ8Result,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_lab_check import (
    EnumLabCheckVerdict,
)

_NODE = "node_pr_landing_decision_compute"
_SECTION = "join_j8"


def load_join_j8_policy(contract: Mapping[str, Any] | None = None) -> ModelJoinJ8Policy:
    """The contract's ``join_j8`` section; an absent section or key refuses naming it."""
    if contract is None:
        contract = yaml.safe_load(
            resources.files(f"omnimarket.nodes.{_NODE}")
            .joinpath("contract.yaml")
            .read_text()
        )
    section = contract.get(_SECTION) if contract else None
    if not isinstance(section, dict):
        raise ValueError(f"{_NODE} contract declares no {_SECTION} section")
    for key in ModelJoinJ8Policy.model_fields:
        if key not in section:
            raise ValueError(f"{_NODE} contract {_SECTION}.{key} is absent")
    return ModelJoinJ8Policy(**section)


class HandlerLandingJoinJ8:
    def __init__(
        self,
        *,
        sink: ProtocolEmissionSink | None = None,
        policy: ModelJoinJ8Policy | None = None,
        clock: Callable[[], datetime] | None = None,
        backend: Literal["in_memory", "real"] = "real",
    ) -> None:
        self._sink = sink
        self._policy = policy if policy is not None else load_join_j8_policy()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._backend = backend

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    def handle(self, request: ModelJoinJ8Request) -> ModelJoinJ8Result:
        facts = request.facts
        pr = next((p for p in facts.prs if p.pr == request.pr), None)
        if pr is None:
            raise ValueError(f"{request.pr} is not in the facts")
        reading = read_lab_check(pr, facts.observed_at, facts.policy)
        if reading is None:
            raise ValueError(f"{request.pr} carries no lab_check fact to join on")
        cid = request.correlation_id
        policy = self._policy
        refusals = {
            EnumLabCheckVerdict.MISSING: (
                EnumJoinJ8Outcome.HELD_MISSING,
                policy.missing_failure_code,
            ),
            EnumLabCheckVerdict.SUPERSEDED: (
                EnumJoinJ8Outcome.HELD_SUPERSEDED,
                policy.superseded_failure_code,
            ),
            EnumLabCheckVerdict.FAILED: (
                EnumJoinJ8Outcome.HELD_FAILED,
                policy.failed_failure_code,
            ),
        }
        if reading.verdict is EnumLabCheckVerdict.PENDING:
            return ModelJoinJ8Result(
                outcome=EnumJoinJ8Outcome.HELD_PENDING,
                correlation_id=cid,
                detail=reading.detail,
                skip_reason="lab_check_pending",
            )
        if reading.verdict in refusals:
            outcome, code = refusals[reading.verdict]
            self._emit(
                cid, "failure", failure_code=code, failure_detail=reading.detail[:300]
            )
            return ModelJoinJ8Result(
                outcome=outcome,
                correlation_id=cid,
                failure_code=code,
                detail=reading.detail,
                skip_reason=f"lab_check_{reading.verdict.value}",
            )

        self._emit(cid, "start")
        decision = decide_landing(facts)
        merged = any(
            a.kind is EnumLandingActionKind.MERGE
            and a.subject == request.pr
            and a.head_sha == pr.head_sha
            for a in decision.actions
        )
        if merged:
            self._emit(cid, "success", counters={"merges_decided": 1})
            return ModelJoinJ8Result(
                outcome=EnumJoinJ8Outcome.MERGEABLE,
                correlation_id=cid,
                detail=reading.detail,
                merge_head=pr.head_sha,
            )
        skip = next(
            (s.reason.value for s in decision.land_skips if s.pr == request.pr), None
        )
        code = policy.not_merged_failure_code
        detail = f"the lab check passed and the decision still held the head: {skip or 'not a landing candidate'}"
        self._emit(cid, "failure", failure_code=code, failure_detail=detail[:300])
        return ModelJoinJ8Result(
            outcome=EnumJoinJ8Outcome.HELD_OTHER,
            correlation_id=cid,
            failure_code=code,
            detail=detail,
            skip_reason=skip,
        )

    def _emit(self, cid: str, phase: str, **extra: object) -> None:
        if self._sink is None:
            raise ValueError(
                "HandlerLandingJoinJ8 was built without an emission sink; inject one to emit"
            )
        self._sink.emit(
            ModelNodeEmission.model_validate(
                {
                    "node": self._policy.emission_node,
                    "piece": self._policy.to_piece,
                    "correlation_id": cid,
                    "phase": phase,
                    "emitted_at": self._clock(),
                    "backend": self._backend,
                    "drill": False,
                    **extra,
                }
            )
        )


__all__ = ["HandlerLandingJoinJ8", "load_join_j8_policy"]
