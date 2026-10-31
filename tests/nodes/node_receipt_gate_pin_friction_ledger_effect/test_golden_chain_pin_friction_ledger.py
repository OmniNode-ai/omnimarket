# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_receipt_gate_pin_friction_ledger_effect: a friction event as a FRICTION ledger row [OMN-20134].

The row must be one the existing friction readers count: a FRICTION row, a
class=pin-advance cell, the site, a parseable cost, a ticket. The appender is a
fake that answers each way the ledger host can.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
import yaml

from omnimarket.models.receipt_gate_pin_friction import (
    EnumPinFrictionReason,
    EnumPinKind,
    EnumPinRefusalClass,
    ModelPinFrictionEvent,
    ModelPinFrictionFoldResult,
)
from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_friction_rollup_compute.handlers import (
    friction_rollup_core as rollup,
)
from omnimarket.nodes.node_receipt_gate_pin_friction_ledger_effect.handlers.handler_pin_friction_ledger import (
    HandlerReceiptGatePinFrictionLedger,
    build_friction_row,
    load_ledger_config,
    request_id_of,
)
from omnimarket.nodes.node_receipt_gate_pin_friction_ledger_effect.models import (
    EnumPinFrictionLedgerStatus,
    ModelPinFrictionLedgerAppended,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_receipt_gate_pin_friction_ledger_effect"
)
CONFIG = load_ledger_config()
NOW = datetime(2026, 10, 31, 2, 0, 0, tzinfo=UTC)
TAG = "dev-20261030152335-66d35d2283c3"


def _event(**fields: object) -> ModelPinFrictionEvent:
    base: dict[str, object] = {
        "reason": EnumPinFrictionReason.HUMAN_TOUCHED,
        "site": "OmniNode-ai/omnibase_infra",
        "pin_kind": EnumPinKind.SHA,
        "core_tag": TAG,
        "branch": f"pin-advance/receipt-gate/{TAG}",
        "pr_number": 5477,
        "fix_pushes": 1,
        "ci_reruns": 2,
        "refusal_classes": (EnumPinRefusalClass.AUDIT,),
        "detail": "1 fix push(es) and 2 CI re-run(s) after the updater",
        "flagged_at": NOW - timedelta(minutes=5),
    }
    return ModelPinFrictionEvent.model_validate({**base, **fields})


class FakeAppender:
    def __init__(
        self,
        status: EnumWorkLedgerAppendStatus | None = EnumWorkLedgerAppendStatus.ACCEPTED,
        *,
        answer_for: UUID | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.status = status
        self.answer_for = answer_for
        self.raises = raises
        self.requests: list[ModelWorkLedgerAppendRequest] = []

    async def append(
        self, request: ModelWorkLedgerAppendRequest, *, timeout_s: float
    ) -> ModelWorkLedgerAppendReceipt | None:
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        if self.status is None:
            return None
        return ModelWorkLedgerAppendReceipt(
            request_id=self.answer_for or request.request_id,
            status=self.status,
            exit_code=0 if self.status is EnumWorkLedgerAppendStatus.ACCEPTED else 65,
            message="planted",
            ledger_lines=[3701],
            ledger_host="ledger-host",
            duration_ms=4,
        )


def _handle(
    appender: FakeAppender | None, event: ModelPinFrictionEvent | None = None
) -> ModelPinFrictionLedgerAppended:
    handler = HandlerReceiptGatePinFrictionLedger(appender, "h200", clock=lambda: NOW)
    if appender is None:
        handler._appender = None
    answered = asyncio.run(
        handler.handle(ModelPinFrictionFoldResult(friction_event=event or _event()))
    )
    assert answered is not None
    return answered


def test_the_row_is_a_friction_row_the_existing_readers_count_by_class_and_site() -> (
    None
):
    request_id = request_id_of(_event(), CONFIG)
    row = build_friction_row(
        _event(), config=CONFIG, stamp=NOW, request_id=request_id, host="h200"
    )
    assert row.startswith(
        "2026-10-31T02:00:00Z | FRICTION | lane=receipt-gate-pin-advance | "
    )
    cells = row.split(" | ")
    assert "class=pin-advance" in cells
    assert "site=OmniNode-ai/omnibase_infra" in cells
    assert "pin_kind=sha" in cells
    assert "refusals=audit" in cells
    assert "existing=OMN-20134" in cells
    assert f"req={request_id}" in cells
    assert "via=bus:h200" in cells
    assert "pr=omnibase_infra#5477" in cells

    parsed = rollup.parse_markdown_rows(row + "\n", "planted")
    assert len(parsed) == 1
    assert parsed[0].row_type == "FRICTION"
    assert parsed[0].first("class") == "pin-advance"
    assert parsed[0].first("site") == "OmniNode-ai/omnibase_infra"
    assert rollup.parse_cost_minutes(parsed[0].first("cost")) == 10.0
    observation = rollup._observation(parsed[0], {}, frozenset())
    assert observation.effective_class == "pin-advance"
    assert observation.cost_minutes == 10.0
    assert observation.owning_tickets == frozenset({"OMN-20134"})
    assert rollup._is_friction(parsed[0])


def test_an_updater_failure_row_carries_its_reason_and_cost_and_no_pr() -> None:
    event = _event(
        reason=EnumPinFrictionReason.UPDATER_FAILED,
        pr_number=None,
        fix_pushes=0,
        ci_reruns=0,
        refusal_classes=(EnumPinRefusalClass.GUARD, EnumPinRefusalClass.CONFLICT),
        detail="pr_create was refused: quota_floor | forged cell\nforged row",
    )
    row = build_friction_row(
        event,
        config=CONFIG,
        stamp=NOW,
        request_id=request_id_of(event, CONFIG),
        host="h200",
    )
    cells = row.split(" | ")
    assert "reason=updater-failed" in cells
    assert "refusals=guard,conflict" in cells
    assert "cost=~15 lane-minutes" in cells
    assert not any(c.startswith("pr=") for c in cells)
    assert "\n" not in row
    assert "forged cell" in cells[-1]
    assert len(cells) == 19


def test_the_request_id_is_stable_per_site_branch_and_reason() -> None:
    a = request_id_of(_event(), CONFIG)
    assert a == request_id_of(_event(ci_reruns=5), CONFIG)
    assert a != request_id_of(
        _event(reason=EnumPinFrictionReason.UPDATER_FAILED), CONFIG
    )
    assert a != request_id_of(_event(site="OmniNode-ai/omnimarket"), CONFIG)
    assert a != request_id_of(_event(branch="pin-advance/receipt-gate/other"), CONFIG)


def test_an_accepted_append_is_answered_accepted_with_the_ledger_line() -> None:
    appender = FakeAppender()
    answer = _handle(appender)
    assert answer.status is EnumPinFrictionLedgerStatus.ACCEPTED
    assert answer.ledger_lines == (3701,)
    assert answer.ledger_host == "ledger-host"
    assert len(appender.requests) == 1
    request = appender.requests[0]
    assert request.request_id == answer.request_id
    assert request.requested_by_lane == "receipt-gate-pin-advance"
    assert request.rows.endswith("\n")
    assert request.rows.strip() == answer.row


def test_a_duplicate_a_refusal_and_an_error_are_each_answered_typed() -> None:
    assert (
        _handle(FakeAppender(EnumWorkLedgerAppendStatus.DUPLICATE)).status
        is EnumPinFrictionLedgerStatus.DUPLICATE
    )
    assert (
        _handle(FakeAppender(EnumWorkLedgerAppendStatus.REFUSED)).status
        is EnumPinFrictionLedgerStatus.REFUSED
    )
    assert (
        _handle(FakeAppender(EnumWorkLedgerAppendStatus.ERROR)).status
        is EnumPinFrictionLedgerStatus.ERROR
    )


def test_no_receipt_is_pending_and_a_bus_failure_is_an_error() -> None:
    assert _handle(FakeAppender(None)).status is EnumPinFrictionLedgerStatus.PENDING
    failed = _handle(FakeAppender(raises=OSError("broker down")))
    assert failed.status is EnumPinFrictionLedgerStatus.ERROR
    assert "broker down" in failed.message


def test_a_receipt_for_another_request_is_an_error() -> None:
    answer = _handle(FakeAppender(answer_for=UUID(int=1)))
    assert answer.status is EnumPinFrictionLedgerStatus.ERROR
    assert "answered for request" in answer.message


def test_a_fold_that_caused_no_friction_appends_nothing() -> None:
    appender = FakeAppender()
    handler = HandlerReceiptGatePinFrictionLedger(appender, "h200", clock=lambda: NOW)
    assert asyncio.run(handler.handle(ModelPinFrictionFoldResult())) is None
    assert appender.requests == []


def test_with_no_appender_wired_the_event_is_answered_error_never_silence() -> None:
    answer = _handle(None)
    assert answer.status is EnumPinFrictionLedgerStatus.ERROR
    assert "no ledger appender is wired" in answer.message
    assert answer.row.startswith("2026-10-31T02:00:00Z | FRICTION | ")


def test_the_contract_subscribes_to_the_friction_topic_the_projection_publishes() -> (
    None
):
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    projection = yaml.safe_load(
        (
            NODE_DIR.parent
            / "node_projection_receipt_gate_pin_friction"
            / "contract.yaml"
        ).read_text()
    )
    assert (
        contract["event_bus"]["subscribe_topics"]
        == projection["event_bus"]["publish_topics"]
    )
    assert contract["pin_friction_ledger_config"]["friction_class"] == "pin-advance"
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.receipt-gate-pin-friction-appended.v1"
    )
    assert contract["event_bus"]["publish_topics"] == [contract["terminal_event"]]
    assert set(CONFIG.cost_by_reason) == set(EnumPinFrictionReason)
