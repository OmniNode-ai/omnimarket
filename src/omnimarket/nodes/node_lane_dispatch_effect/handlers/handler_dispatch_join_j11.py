# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J11 (piece 11 to piece 12): a runtime-affecting merge's PASS lab receipt is consumed by dispatch, and a lane starts
from the dispatch record (OMN-22961).

Canonical shape: ``handle(ModelJoinJ11Request) -> ModelJoinJ11Result``. For one merge:

* a merge that is not runtime-affecting needs no receipt: the join is exempt and nothing starts;
* a PASS lab pass receipt on the required lane for the exact merged sha, finished after the merge and no later than
  the judging time, is consumed: the one dispatch spec is sent through ``HandlerLaneDispatch`` (the existing dispatch
  effect, as a list of one so the lane-matched placement acknowledgement decides) and the lane starts from the
  immutable job record that effect writes;
* a receipt that is missing, a FAIL or unreadable starts nothing: the dispatch effect is never called and no record is
  written. A dispatch that is refused, still placing or crashed is not a started lane either.

Everything else is injected: the receipt source and the dispatch effect. The reason codes are the contract's
``join_j11`` section.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib import resources
from typing import Any, Protocol, runtime_checkable

import yaml

from omnimarket.models.model_dev_deploy_reading import ModelLabPassReceiptFact
from omnimarket.nodes.node_lane_dispatch_effect.handlers.handler_lane_dispatch import (
    HandlerLaneDispatch,
)
from omnimarket.nodes.node_lane_dispatch_effect.models.model_join_j11 import (
    EnumJoinJ11Outcome,
    ModelJoinJ11Policy,
    ModelJoinJ11Request,
    ModelJoinJ11Result,
)
from omnimarket.nodes.node_lane_dispatch_effect.models.model_lane_dispatch_effect import (
    ModelLaneDispatchRequest,
)

_NODE = "node_lane_dispatch_effect"
_SECTION = "join_j11"
_SHORT = 12


@runtime_checkable
class ProtocolLabPassReceiptSource(Protocol):
    """Every lab pass receipt found for one exact sha, or None when the store could not be read."""

    def read_receipts(self, sha: str) -> tuple[ModelLabPassReceiptFact, ...] | None: ...


def load_join_j11_policy(
    contract: Mapping[str, Any] | None = None,
) -> ModelJoinJ11Policy:
    """The contract's ``join_j11`` section; an absent section or key refuses naming it."""
    if contract is None:
        contract = yaml.safe_load(
            resources.files(f"omnimarket.nodes.{_NODE}")
            .joinpath("contract.yaml")
            .read_text()
        )
    section = contract.get(_SECTION) if contract else None
    if not isinstance(section, dict):
        raise ValueError(f"{_NODE} contract declares no {_SECTION} section")
    for key in ModelJoinJ11Policy.model_fields:
        if key not in section:
            raise ValueError(f"{_NODE} contract {_SECTION}.{key} is absent")
    return ModelJoinJ11Policy(**section)


class HandlerDispatchJoinJ11:
    """Dispatch consumes the PASS receipt: no PASS receipt, no dispatch call."""

    def __init__(
        self,
        *,
        receipts: ProtocolLabPassReceiptSource,
        dispatch: HandlerLaneDispatch,
        policy: ModelJoinJ11Policy,
    ) -> None:
        self._receipts = receipts
        self._dispatch = dispatch
        self._policy = policy

    def handle(self, request: ModelJoinJ11Request) -> ModelJoinJ11Result:
        base = {
            "correlation_id": request.correlation_id,
            "merged_sha": request.merged_sha,
        }
        if not request.runtime_affecting:
            return ModelJoinJ11Result(outcome=EnumJoinJ11Outcome.EXEMPT, **base)
        unmet = self._receipt_unmet(request)
        if unmet is not None:
            code, detail = unmet
            return ModelJoinJ11Result(
                outcome=EnumJoinJ11Outcome.NOT_STARTED,
                reason_code=code,
                detail=detail,
                **base,
            )
        batch = self._dispatch.handle(
            ModelLaneDispatchRequest(
                record_json=json.dumps([json.loads(request.record_json)]),
                output_dir=request.output_dir,
                execute=True,
            )
        )
        item = batch.results[0] if batch.results else None
        if item is not None and item.status == "placed":
            return ModelJoinJ11Result(
                outcome=EnumJoinJ11Outcome.STARTED, dispatch=item, **base
            )
        return ModelJoinJ11Result(
            outcome=EnumJoinJ11Outcome.NOT_STARTED,
            reason_code=self._policy.dispatch_not_placed_code,
            detail=item.reason if item is not None else batch.detail,
            dispatch=item,
            **base,
        )

    def _receipt_unmet(self, request: ModelJoinJ11Request) -> tuple[str, str] | None:
        """Why the exact sha has no consumable PASS receipt, or None when it has one."""
        policy = self._policy
        tag = request.merged_sha[:_SHORT]
        read = self._receipts.read_receipts(request.merged_sha)
        if read is None:
            return (
                policy.receipts_unreadable_code,
                f"the lab pass receipts for {tag} could not be read",
            )
        own = [
            r
            for r in read
            if r.sha == request.merged_sha
            and r.lane == request.required_lane
            and request.merged_at <= r.finished_at <= request.judged_at
        ]
        if any(r.result == "PASS" for r in own):
            return None
        if any(r.result == "FAIL" for r in own):
            return (
                policy.receipt_failed_code,
                f"lab pass receipt for {tag} on lane {request.required_lane} reads FAIL",
            )
        return (
            policy.receipt_missing_code,
            f"no PASS lab pass receipt for {tag} on lane {request.required_lane} since {request.merged_at.isoformat()}",
        )


__all__ = [
    "HandlerDispatchJoinJ11",
    "ProtocolLabPassReceiptSource",
    "load_join_j11_policy",
]
