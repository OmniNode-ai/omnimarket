# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Map one landing decision to its ledger STATUS rows (OMN-17427)."""

from datetime import UTC
from typing import Literal
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

from omnimarket.models.work_ledger_append import ModelWorkLedgerAppendRequest
from omnimarket.nodes.node_work_ledger_landing_mirror.models.model_work_ledger_landing_mirror import (
    ModelWorkLedgerLandingMirrorRequest,
    ModelWorkLedgerLandingMirrorResult,
)

LANE = "landing-controller"


def _cell(value: str) -> str:
    """Make a decision's free text one safe ledger cell.

    ``subject`` is not pattern-bound on an action or a DEGRADED reason, so it
    may carry the cell separator or a newline; either would end the cell or
    forge a second row. Percent-encoding keeps spaces and the characters the
    pattern-bound subjects use, and encodes everything else.
    """
    return quote(value, safe="-_.:/#@ ")


class HandlerWorkLedgerLandingMirror:
    """Definition-B compute: no file, clock, bus or model calls.

    A tick that performed no action, recorded no outcome and set no DEGRADED
    reason or violation maps to no request, so an idle tick writes no row.
    Observe-only actions were withheld, not done, and get no row either.
    """

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    def handle(
        self, request: ModelWorkLedgerLandingMirrorRequest
    ) -> ModelWorkLedgerLandingMirrorResult:
        decision = request.decision
        # The event, rather than the tick number or the clock, is the identity:
        # a redelivery of one event maps to one request.
        request_id = uuid5(
            NAMESPACE_URL, f"onex:work-ledger:landing:{request.correlation_id}"
        )
        stamp = request.emitted_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        head = f"{stamp} | STATUS | lane={LANE} | src=landing | tick={decision.tick}"
        tail = f"req={request_id} | via=bus:landing"
        rows: list[str] = []
        for action in decision.actions:
            cells = [
                head,
                f"action={action.kind.value}",
                f"subject={_cell(action.subject)}",
            ]
            if action.head_sha is not None:
                cells.append(f"head={action.head_sha}")
            if action.lease_id is not None:
                cells.append(f"lease=L{action.lease_id}")
            cells += [tail, "Landing controller action decided"]
            rows.append(" | ".join(cells))
        for outcome in decision.recorded_outcomes:
            cells = [
                head,
                f"subject={_cell(outcome.pr)}",
                f"outcome={outcome.outcome.value}",
            ]
            cells.append(f"reason={outcome.reason.value}")
            if outcome.lease_id is not None:
                cells.append(f"lease=L{outcome.lease_id}")
            cells += [tail, "Landing controller outcome recorded"]
            rows.append(" | ".join(cells))
        for degraded in decision.degraded:
            rows.append(
                " | ".join(
                    [
                        head,
                        f"degraded={degraded.reason.value}",
                        f"subject={_cell(degraded.subject)}",
                        tail,
                        "Landing controller DEGRADED reason set",
                    ]
                )
            )
        for violation in decision.violations:
            rows.append(
                " | ".join(
                    [
                        head,
                        f"violation={violation.reason.value}",
                        f"subject={_cell(violation.pr)}",
                        f"lease=L{violation.lease_id}",
                        tail,
                        "Landing controller result failed verification",
                    ]
                )
            )
        if not rows:
            return ModelWorkLedgerLandingMirrorResult(row_count=0)
        return ModelWorkLedgerLandingMirrorResult(
            request=ModelWorkLedgerAppendRequest(
                request_id=request_id,
                rows="\n".join(rows),
                requested_by_lane=LANE,
                requesting_host="landing-bus",
                requested_at=request.emitted_at,
            ),
            row_count=len(rows),
        )
