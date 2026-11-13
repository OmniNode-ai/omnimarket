# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J9 (piece 9 to piece 10): a merged fix is installed on every target host within an hour and read back equal.

Canonical shape: ``handle(ModelJoinJ9Request) -> ModelJoinJ9Result``. The handler reads the receipts the node's own
tick wrote (``ModelFixInstallReceipt``, each with the time it was written) against the head piece 9 merged:

* every declared target holds a receipt written after the merge and inside the deadline whose status is ``installed``
  or ``current`` and whose read-back revision equals the merged head: a start then a success on piece 10;
* a target still without such a receipt while the deadline has not passed: nothing is emitted, the join is not judged;
* the deadline has passed and a target is still without it: each such target is named in a typed failure (late, a
  read-back that differs, or a refused or failed install) and piece 10 emits a start then a failure.

Everything else is injected: the emission sink and the clock. Its settings are the contract's ``join_j9`` section.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Any, Literal

import yaml

from omnimarket.handlers.emission_sink import ProtocolEmissionSink
from omnimarket.models.floor_17a import ModelNodeEmission
from omnimarket.nodes.node_automatic_fix_install_effect.models import (
    EnumJoinJ9Outcome,
    ModelJoinJ9Failure,
    ModelJoinJ9Policy,
    ModelJoinJ9Reading,
    ModelJoinJ9Request,
    ModelJoinJ9Result,
    ModelJoinJ9Target,
)

_NODE = "node_automatic_fix_install_effect"
_SECTION = "join_j9"
_INSTALLED_STATUSES = frozenset({"installed", "current"})


def load_join_j9_policy(contract: Mapping[str, Any] | None = None) -> ModelJoinJ9Policy:
    """The contract's ``join_j9`` section; an absent section or key refuses naming it."""
    if contract is None:
        contract = yaml.safe_load(
            resources.files(f"omnimarket.nodes.{_NODE}")
            .joinpath("contract.yaml")
            .read_text()
        )
    section = contract.get(_SECTION) if contract else None
    if not isinstance(section, dict):
        raise ValueError(f"{_NODE} contract declares no {_SECTION} section")
    for key in ModelJoinJ9Policy.model_fields:
        if key not in section:
            raise ValueError(f"{_NODE} contract {_SECTION}.{key} is absent")
    return ModelJoinJ9Policy(**section)


class HandlerFixInstallJoinJ9:
    def __init__(
        self,
        *,
        sink: ProtocolEmissionSink,
        policy: ModelJoinJ9Policy,
        clock: Callable[[], datetime] | None = None,
        backend: Literal["in_memory", "real"] = "real",
    ) -> None:
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

    def handle(self, request: ModelJoinJ9Request) -> ModelJoinJ9Result:
        policy = self._policy
        deadline_at = request.merged_at + timedelta(seconds=policy.deadline_seconds)
        past_deadline = request.judged_at > deadline_at
        installed: list[str] = []
        pending: list[str] = []
        failures: list[ModelJoinJ9Failure] = []
        for target in request.targets:
            failure = self._unmet(target, request, deadline_at)
            if failure is None:
                installed.append(target.key)
            elif past_deadline:
                failures.append(failure)
            else:
                pending.append(target.key)
        cid = request.correlation_id
        base = {
            "correlation_id": cid,
            "merged_head": request.merged_head,
            "deadline_at": deadline_at,
        }
        if failures:
            first = failures[0]
            hosts = ", ".join(f"{f.host} ({f.code})" for f in failures)
            self._emit(cid, "start")
            self._emit(
                cid,
                "failure",
                failure_code=first.code,
                failure_detail=f"not installed within the deadline: {hosts}"[:300],
            )
            return ModelJoinJ9Result(
                outcome=EnumJoinJ9Outcome.FAILED,
                installed=tuple(installed),
                failures=tuple(failures),
                failure_code=first.code,
                **base,
            )
        if pending:
            return ModelJoinJ9Result(
                outcome=EnumJoinJ9Outcome.PENDING,
                installed=tuple(installed),
                pending=tuple(pending),
                **base,
            )
        self._emit(cid, "start")
        self._emit(cid, "success", counters={"targets_installed": len(installed)})
        return ModelJoinJ9Result(
            outcome=EnumJoinJ9Outcome.INSTALLED, installed=tuple(installed), **base
        )

    def _unmet(
        self,
        target: ModelJoinJ9Target,
        request: ModelJoinJ9Request,
        deadline_at: datetime,
    ) -> ModelJoinJ9Failure | None:
        """Why this target does not yet hold the merged head, or None when it does within the deadline."""
        policy = self._policy

        def failure(code: str, detail: str) -> ModelJoinJ9Failure:
            return ModelJoinJ9Failure(
                key=target.key, host=target.host, code=code, detail=detail
            )

        # A receipt written before the merge says nothing about the merged head.
        after_merge = [
            r
            for r in request.readings
            if r.receipt.key == target.key and r.at >= request.merged_at
        ]
        if not after_merge:
            return failure(
                policy.late_failure_code,
                f"no install receipt after the merge at {request.merged_at.isoformat()}",
            )
        # The first receipt that reads back the merged head decides the time; later ones do not undo it.
        good = [r for r in after_merge if self._reads_back(r, request.merged_head)]
        if good:
            first = min(good, key=lambda r: r.at)
            if first.at > deadline_at:
                return failure(
                    policy.late_failure_code,
                    f"installed at {first.at.isoformat()}, after the deadline {deadline_at.isoformat()}",
                )
            return None
        last = max(after_merge, key=lambda r: r.at).receipt
        if last.status in {"failed", "refused"}:
            return failure(
                policy.refused_failure_code,
                f"install {last.status}: {last.reason or 'no reason given'}",
            )
        return failure(
            policy.mismatch_failure_code,
            f"status {last.status}, read back {last.installed or 'nothing'}, merged head {request.merged_head}",
        )

    @staticmethod
    def _reads_back(reading: ModelJoinJ9Reading, merged_head: str) -> bool:
        receipt = reading.receipt
        return (
            receipt.status in _INSTALLED_STATUSES
            and receipt.installed.lower() == merged_head
        )

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
                    "origin": "probe",
                    **extra,
                }
            )
        )


__all__ = ["HandlerFixInstallJoinJ9", "load_join_j9_policy"]
