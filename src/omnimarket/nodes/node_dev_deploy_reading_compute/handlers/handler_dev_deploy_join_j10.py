# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J10 (piece 10 to piece 11): a runtime-affecting merge yields a PASS lab receipt keyed by its sha, and the running
runtime reads back that exact sha and answers one correlated functional request (OMN-22957).

Rebuild plan 4.1, J10. Canonical shape: ``handle(ModelJoinJ10Request) -> ModelJoinJ10Result``. For one merge:

* a merge that is not runtime-affecting needs neither: the join is exempt and emits nothing;
* a PASS lab pass receipt on the required lane for the exact merged sha, finished after the merge and inside the
  deadline, must exist, and the runtime, asked one request carrying the join's correlation id, must answer
  functionally, report the merged sha and echo the correlation id: piece 11 emits a start then a success;
* a PASS receipt beside a runtime that is stale (another sha), broken (unreachable, not functional, no sha) or
  uncorrelated (another or no correlation id) fails the join, as does a receipt that is missing, late, a FAIL or
  unreadable. Inside the deadline each of these is PENDING (the runtime may still be rebuilding): nothing is emitted.
  Past the deadline piece 11 emits a start then a typed failure naming the cause.

Everything else is injected: the receipt reader, the runtime probe, the emission sink and the clock. The failure codes
are the contract's ``join_j10`` section.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Any, Literal

import yaml

from omnimarket.handlers.emission_sink import ProtocolEmissionSink
from omnimarket.models.floor_17a import ModelNodeEmission
from omnimarket.nodes.node_dev_deploy_reading_compute.models.model_dev_deploy_reading import (
    ModelLabPassReceiptFact,
)
from omnimarket.nodes.node_dev_deploy_reading_compute.models.model_join_j10 import (
    EnumJoinJ10Outcome,
    ModelJoinJ10Policy,
    ModelJoinJ10Request,
    ModelJoinJ10Result,
    ModelRuntimeAnswer,
    ModelRuntimeAsk,
)
from omnimarket.nodes.node_dev_deploy_reading_compute.protocols.protocol_dev_deploy_join import (
    ProtocolLabPassReceiptReader,
    ProtocolRuntimeProbe,
)

_NODE = "node_dev_deploy_reading_compute"
_SECTION = "join_j10"
_SHORT = 12


def load_join_j10_policy(
    contract: Mapping[str, Any] | None = None,
) -> ModelJoinJ10Policy:
    """The contract's ``join_j10`` section; an absent section or key refuses naming it."""
    if contract is None:
        contract = yaml.safe_load(
            resources.files(f"omnimarket.nodes.{_NODE}")
            .joinpath("contract.yaml")
            .read_text()
        )
    section = contract.get(_SECTION) if contract else None
    if not isinstance(section, dict):
        raise ValueError(f"{_NODE} contract declares no {_SECTION} section")
    for key in ModelJoinJ10Policy.model_fields:
        if key not in section:
            raise ValueError(f"{_NODE} contract {_SECTION}.{key} is absent")
    return ModelJoinJ10Policy(**section)


class HandlerDevDeployJoinJ10:
    def __init__(
        self,
        *,
        receipts: ProtocolLabPassReceiptReader,
        runtime: ProtocolRuntimeProbe,
        sink: ProtocolEmissionSink,
        policy: ModelJoinJ10Policy,
        clock: Callable[[], datetime] | None = None,
        backend: Literal["in_memory", "real"] = "real",
    ) -> None:
        self._receipts = receipts
        self._runtime = runtime
        self._sink = sink
        self._policy = policy
        self._clock = clock or (lambda: datetime.now(UTC))
        self._backend = backend

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    def handle(self, request: ModelJoinJ10Request) -> ModelJoinJ10Result:
        deadline_at = request.merged_at + timedelta(seconds=request.deadline_seconds)
        base = {
            "correlation_id": request.correlation_id,
            "merged_sha": request.merged_sha,
            "deadline_at": deadline_at,
        }
        if not request.runtime_affecting:
            return ModelJoinJ10Result(outcome=EnumJoinJ10Outcome.EXEMPT, **base)
        unmet = self._receipt_unmet(request, deadline_at)
        observed: str | None = None
        if unmet is None:
            answer = self._runtime.ask(
                ModelRuntimeAsk(
                    correlation_id=request.correlation_id,
                    expected_sha=request.merged_sha,
                )
            )
            observed = answer.observed_sha
            unmet = self._runtime_unmet(request, answer)
        if unmet is None:
            self._emit(request.correlation_id, "start")
            self._emit(request.correlation_id, "success", counters={"runtime_reads": 1})
            return ModelJoinJ10Result(
                outcome=EnumJoinJ10Outcome.JOINED,
                runtime_observed_sha=observed,
                **base,
            )
        code, detail = unmet
        if request.judged_at <= deadline_at:
            return ModelJoinJ10Result(
                outcome=EnumJoinJ10Outcome.PENDING,
                failure_code=code,
                failure_detail=detail,
                runtime_observed_sha=observed,
                **base,
            )
        self._emit(request.correlation_id, "start")
        self._emit(
            request.correlation_id,
            "failure",
            failure_code=code,
            failure_detail=detail[:300],
        )
        return ModelJoinJ10Result(
            outcome=EnumJoinJ10Outcome.FAILED,
            failure_code=code,
            failure_detail=detail,
            runtime_observed_sha=observed,
            **base,
        )

    def _receipt_unmet(
        self, request: ModelJoinJ10Request, deadline_at: datetime
    ) -> tuple[str, str] | None:
        """Why the exact sha has no PASS receipt inside the deadline, or None when it has one."""
        policy = self._policy
        tag = request.merged_sha[:_SHORT]
        read = self._receipts.read_receipts(request.merged_sha)
        if read.unread_reason is not None:
            return policy.receipts_unreadable_code, read.unread_reason
        own: list[ModelLabPassReceiptFact] = [
            r
            for r in read.receipts
            if r.sha == request.merged_sha
            and r.lane == request.required_lane
            and r.finished_at <= request.judged_at
        ]
        passes = sorted(
            r.finished_at
            for r in own
            if r.result == "PASS" and r.finished_at >= request.merged_at
        )
        if passes and passes[0] <= deadline_at:
            return None
        if passes:
            return (
                policy.receipt_late_code,
                f"PASS receipt for {tag} finished {passes[0].isoformat()}, after the deadline {deadline_at.isoformat()}",
            )
        if any(r.result == "FAIL" for r in own):
            return (
                policy.receipt_failed_code,
                f"lab pass receipt for {tag} on lane {request.required_lane} reads FAIL",
            )
        return (
            policy.receipt_missing_code,
            f"no PASS lab pass receipt for {tag} on lane {request.required_lane} since {request.merged_at.isoformat()}",
        )

    def _runtime_unmet(
        self, request: ModelJoinJ10Request, answer: ModelRuntimeAnswer
    ) -> tuple[str, str] | None:
        """Why the running runtime does not answer as the merged sha, or None when it does."""
        policy = self._policy
        tag = request.merged_sha[:_SHORT]
        if not answer.reachable:
            return (
                policy.runtime_broken_code,
                f"runtime gave no answer: {answer.detail}",
            )
        if not answer.functional_ok:
            return (
                policy.runtime_broken_code,
                f"runtime answered not functional: {answer.detail}",
            )
        if answer.observed_sha is None:
            return policy.runtime_broken_code, "runtime reported no sha"
        if answer.observed_sha != request.merged_sha:
            return (
                policy.runtime_stale_code,
                f"runtime reads back {answer.observed_sha[:_SHORT]}, merged sha {tag}",
            )
        if answer.echoed_correlation_id != request.correlation_id:
            return (
                policy.runtime_uncorrelated_code,
                f"runtime echoed correlation id {answer.echoed_correlation_id!r}, asked {request.correlation_id!r}",
            )
        return None

    def _emit(self, cid: str, phase: str, **extra: object) -> None:
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


__all__ = ["HandlerDevDeployJoinJ10", "load_join_j10_policy"]
