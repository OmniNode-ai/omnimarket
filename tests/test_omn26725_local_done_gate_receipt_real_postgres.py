# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres write path: a verdict carrying a local Done gate receipt (OMN-26725).

The verdict projection writer upserts the verdict row, and the receipt store
node's handler keeps the receipt a local_done_gate run published on the same
event. This drives the writer against a real migrated schema
(INTEGRATION_POSTGRES_* DSN, skipped when unreachable) and the handler on the
same event, and checks that the verdict row and the stored receipt both land,
and that a verdict without a receipt stores none.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.models.model_dod_verify_completed_event import (
    ModelDodVerifyCompletedEvent,
)
from omnimarket.models.model_local_done_gate_receipt_store import (
    FilesystemLocalDoneGateReceiptStore,
)
from omnimarket.models.model_message_meta import MessageMeta
from omnimarket.nodes.node_local_done_gate_receipt_store_effect.handlers.handler_local_done_gate_receipt_store import (
    HandlerLocalDoneGateReceiptStore,
)
from tests.test_omn18900_real_postgres_dod_verdict_write_path import (
    _event,
    _migrated_writer,
)

pytestmark = pytest.mark.integration

TICKET = "OMN-17372"
SHA = "a" * 40
TOPIC = "onex.evt.omnimarket.dod-verify-completed.v1"
RECEIPT = {
    "ticket_id": TICKET,
    "contract_sha": SHA,
    "items": [{"item_id": "dod-1", "verdict": "VERIFIED"}],
    "host": "box",
    "run_id": "r1",
}


def _meta() -> MessageMeta:
    return MessageMeta(partition=0, offset=0, fallback_id="r", topic=TOPIC)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_verdict_row_and_its_receipt_both_land(tmp_path: Path) -> None:
    store = FilesystemLocalDoneGateReceiptStore(tmp_path)
    async with _migrated_writer() as (writer, connection, schema):
        payload = _event(local_done_gate_receipt=RECEIPT)
        report = await writer._project_one_message(TOPIC, payload, _meta())
        assert report["rows_upserted"] == 1
        count = await connection.fetchval(
            f"SELECT count(*) FROM {schema}.dod_verify_runs WHERE ticket_id = $1",
            TICKET,
        )
    assert count == 1
    handler = HandlerLocalDoneGateReceiptStore(store=store)
    assert handler.handle(ModelDodVerifyCompletedEvent(**payload)).stored is True
    stored = store.receipts_for(TICKET)
    assert [(r.contract_sha, r.run_id) for r in stored] == [(SHA, "r1")]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_verdict_without_a_receipt_stores_none(tmp_path: Path) -> None:
    store = FilesystemLocalDoneGateReceiptStore(tmp_path)
    async with _migrated_writer() as (writer, _connection, _schema):
        payload = _event()
        report = await writer._project_one_message(TOPIC, payload, _meta())
        assert report["rows_upserted"] == 1
    handler = HandlerLocalDoneGateReceiptStore(store=store)
    assert handler.handle(ModelDodVerifyCompletedEvent(**payload)).stored is False
    assert store.receipts_for(TICKET) == []
