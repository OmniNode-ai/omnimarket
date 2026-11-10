# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A queued append whose request id is already on the ledger settles as a duplicate; a clash is refused with both digests.

The recorded refusal (TERMINAL of 2026-11-02T02:45:44Z) reads ``REFUSED 65 refused: request id alrea...``: the
ledger host found the request id on the ledger and could not tell a replay from a different row, and gave no
way to tell which. Here the same-payload replay is a duplicate that never reaches ``refused/``, and a different
payload under the same id is refused naming the id and the digest of each side.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from omnimarket.handlers.bus_binding import outbox
from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_work_ledger_append_effect.handlers.handler_work_ledger_append_effect import (
    HandlerWorkLedgerAppendEffect,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    ModelAppendCommandResult,
)

pytestmark = pytest.mark.unit

REQUEST_ID = UUID("3f1c2a9e-5b7d-4c1e-8a2f-6d4e9b0c7a11")
WRITER = "writer-a"


class _FakeLedger:
    """The ledger file as text, with the append command and the reader the handler takes."""

    def __init__(self) -> None:
        self.text = ""

    def append(self, rows: str) -> ModelAppendCommandResult:
        self.text += rows if rows.endswith("\n") else rows + "\n"
        return ModelAppendCommandResult(exit_code=0)

    def read_text(self) -> str:
        return self.text


def _request(
    words: str, *, request_id: UUID = REQUEST_ID
) -> ModelWorkLedgerAppendRequest:
    return ModelWorkLedgerAppendRequest(
        request_id=request_id,
        rows=f"2026-11-02T02:41:00Z | FRICTION | lane=writer-lane | req={request_id} | {words}\n",
        requested_by_lane="writer-lane",
        requesting_host="h-test",
        requested_at=datetime(2026, 11, 2, 2, 41, tzinfo=UTC),
    )


def _digest(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


@pytest.fixture
def ledger() -> _FakeLedger:
    return _FakeLedger()


@pytest.fixture
def handler(ledger: _FakeLedger) -> HandlerWorkLedgerAppendEffect:
    return HandlerWorkLedgerAppendEffect(
        runner=ledger,
        reader=ledger,
        host_name="ledger-host",
        writer_principals=[WRITER],
    )


def _sender(
    handler: HandlerWorkLedgerAppendEffect,
) -> outbox.Sender:
    async def send(
        request: ModelWorkLedgerAppendRequest,
    ) -> ModelWorkLedgerAppendReceipt:
        return handler.handle(request, writer_principal=WRITER)

    return send


async def test_outbox_request_id_clash_same_payload_settles_as_duplicate(
    tmp_path: Path, handler: HandlerWorkLedgerAppendEffect, ledger: _FakeLedger
) -> None:
    request = _request("the first attempt landed but its receipt was lost")
    assert (
        handler.handle(request, writer_principal=WRITER).status
        is EnumWorkLedgerAppendStatus.ACCEPTED
    )
    outbox.queue_request(tmp_path, request)

    result = await outbox.drain(tmp_path, _sender(handler))

    assert (result.replayed, result.duplicates, result.refused) == (0, 1, 0)
    assert result.remaining == 0
    assert not (outbox.outbox_dir(tmp_path) / outbox.REFUSED_DIRNAME).exists()
    assert ledger.text.count(f"req={REQUEST_ID}") == 1


async def test_outbox_request_id_clash_different_payload_is_refused_with_digests(
    tmp_path: Path, handler: HandlerWorkLedgerAppendEffect, ledger: _FakeLedger
) -> None:
    landed = _request("the row that landed first")
    handler.handle(landed, writer_principal=WRITER)
    queued = _request("a different row under the same request id")
    outbox.queue_request(tmp_path, queued)

    receipt = handler.handle(queued, writer_principal=WRITER)
    assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
    assert receipt.exit_code == 65

    on_ledger = [line for line in ledger.text.splitlines() if line]
    # The ledger side is the stamped row as the ledger holds it; the request side is the row as it would be stamped.
    request_side = [
        line.replace(
            "| lane=writer-lane |", f"| lane=writer-lane | principal={WRITER} |"
        )
        for line in queued.rows.splitlines()
        if line
    ]
    assert str(REQUEST_ID) in receipt.message
    assert _digest(on_ledger) in receipt.message
    assert _digest(request_side) in receipt.message
    assert _digest(on_ledger) != _digest(request_side)

    result = await outbox.drain(tmp_path, _sender(handler))
    assert (result.replayed, result.duplicates, result.refused) == (0, 0, 1)
    refused_file = next(
        (outbox.outbox_dir(tmp_path) / outbox.REFUSED_DIRNAME).glob("*.json")
    )
    assert str(REQUEST_ID) in refused_file.read_text(encoding="utf-8")
    assert ledger.text.count(f"req={REQUEST_ID}") == 1


async def test_outbox_request_id_clash_other_id_is_appended(
    tmp_path: Path, handler: HandlerWorkLedgerAppendEffect, ledger: _FakeLedger
) -> None:
    """Positive control: a different request id with a different payload is not a clash."""
    handler.handle(_request("first"), writer_principal=WRITER)
    other = _request("second", request_id=UUID("9a6d0c3e-1b2f-4d5a-8c7e-0f1a2b3c4d5e"))
    outbox.queue_request(tmp_path, other)

    result = await outbox.drain(tmp_path, _sender(handler))

    assert (result.replayed, result.duplicates, result.refused) == (1, 0, 0)
    assert ledger.text.count("req=") == 2
