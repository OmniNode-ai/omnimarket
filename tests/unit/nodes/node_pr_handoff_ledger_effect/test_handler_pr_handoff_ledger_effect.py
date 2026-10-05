# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_pr_handoff_ledger_effect: stamping, one answer per command, no silence (OMN-20636)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml

from omnimarket.models.pr_handoff import (
    EnumPrHandoffLedgerStatus,
    ModelPrHandoffLedgerAppendCommand,
)
from omnimarket.nodes.node_pr_handoff_ledger_effect.handlers import (
    HandlerPrHandoffLedgerEffect,
)
from omnimarket.nodes.node_pr_handoff_ledger_effect.handlers.handler_pr_handoff_ledger_effect import (
    LEDGER_BUS_LANE_ENV,
    BusLaneLedgerAppender,
    appender_from_environment,
    stamp_rows,
)
from omnimarket.nodes.node_work_ledger_append_effect.models import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from tests.chains.pr_handoff import _builders as b

pytestmark = pytest.mark.unit

_NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_pr_handoff_ledger_effect"
)
REQ = UUID("0b8f7c4e-5a2d-4f61-8b7a-3c9d2e1f0a5b")
ROWS = (
    "2026-10-05T19:00:00Z | MSG | from=lab-lane | to=landing-controller | id=x | "
    "pr=omnimarket#1 | Handoff text.\n"
    "2026-10-05T19:00:00Z | TERMINAL | lane=lab-lane | outcome=handed-off | "
    "delegated=0 delegation_reason=no-text-or-code\n"
)


def _command(attempt: int = 1) -> ModelPrHandoffLedgerAppendCommand:
    return ModelPrHandoffLedgerAppendCommand(
        correlation_id=b.new_cid(),
        handoff_key=b.KEY,
        ledger_request_id=REQ,
        attempt=attempt,
        rows=ROWS,
        requested_by_lane=b.LANE,
        requesting_host="h202",
        requested_at=b.at(0),
    )


class _Raises:
    async def append(
        self, request: ModelWorkLedgerAppendRequest, *, timeout_s: float
    ) -> ModelWorkLedgerAppendReceipt | None:
        raise RuntimeError("no route to the ledger bus")


class _OtherRequest:
    async def append(
        self, request: ModelWorkLedgerAppendRequest, *, timeout_s: float
    ) -> ModelWorkLedgerAppendReceipt | None:
        return ModelWorkLedgerAppendReceipt(
            request_id=b.new_cid(),
            status=EnumWorkLedgerAppendStatus.ACCEPTED,
            exit_code=0,
            message="ok",
            ledger_lines=[1],
            ledger_host="h200",
            duration_ms=1,
        )


def test_stamp_rows_is_onex_ledger_bus_writes_rule() -> None:
    stamped = stamp_rows(ROWS, str(REQ), "h202").splitlines()
    assert stamped[0].endswith(
        f"| pr=omnimarket#1 | req={REQ} | via=bus:h202 | Handoff text."
    )
    # A row whose last cell is a field gains the cells at the end.
    assert stamped[1].endswith(
        f"| delegated=0 delegation_reason=no-text-or-code | req={REQ} | via=bus:h202"
    )


async def test_accepted_receipt_is_answered_with_its_lines() -> None:
    ledger = b.ScriptedLedger([EnumWorkLedgerAppendStatus.ACCEPTED])
    answer = await HandlerPrHandoffLedgerEffect(
        appender=ledger, host_name="h202", clock=lambda: b.at(5)
    ).handle(_command())
    assert answer.status is EnumPrHandoffLedgerStatus.ACCEPTED
    assert answer.ledger_lines == (47200, 47201)
    assert answer.answered_at == b.at(5)
    (sent,) = ledger.requests
    assert sent.request_id == REQ
    assert sent.requesting_host == "h202"
    assert f"req={REQ} | via=bus:h202" in sent.rows


@pytest.mark.parametrize(
    ("appender", "status"),
    [
        (b.ScriptedLedger([None]), EnumPrHandoffLedgerStatus.PENDING),
        (
            b.ScriptedLedger([EnumWorkLedgerAppendStatus.REFUSED]),
            EnumPrHandoffLedgerStatus.REFUSED,
        ),
        (_Raises(), EnumPrHandoffLedgerStatus.ERROR),
        (_OtherRequest(), EnumPrHandoffLedgerStatus.ERROR),
    ],
)
async def test_every_outcome_is_one_typed_answer(
    appender: Any, status: EnumPrHandoffLedgerStatus
) -> None:
    answer = await HandlerPrHandoffLedgerEffect(
        appender=appender, host_name="h202"
    ).handle(_command(attempt=2))
    assert answer.status is status
    assert answer.attempt == 2
    assert answer.handoff_key == b.KEY


async def test_unwired_appender_answers_error_never_silence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(LEDGER_BUS_LANE_ENV, raising=False)
    assert appender_from_environment() is None
    answer = await HandlerPrHandoffLedgerEffect(host_name="h202").handle(_command())
    assert answer.status is EnumPrHandoffLedgerStatus.ERROR
    assert LEDGER_BUS_LANE_ENV in answer.message


def test_environment_wires_the_bus_lane_appender(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(LEDGER_BUS_LANE_ENV, "dev")
    monkeypatch.setenv("OMNIBASE_PATH", str(tmp_path))
    assert isinstance(appender_from_environment(), BusLaneLedgerAppender)


def test_contract_declares_its_one_answer_as_terminal() -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.pr-handoff-ledger-appended.v1"
    ]
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.pr-handoff-ledger-appended.v1"
    )
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.pr-handoff-ledger-append-requested.v1"
    ]
