# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23452 seam: the emit node's row envelope against the ledger projector's writer, over the in-memory bus.

The emit publishes each of the eleven row types on its topic; the test takes the message off the bus and gives it
to ``WorkLedgerProjectionWriter`` the way the runtime does (the payload plus its topic). The projector must write
the row the emit named, under the id and type the emit named. The envelope fields the projector reads are taken
from ``ModelWorkLedgerFoldRequest`` itself, so a field renamed on either side turns the test red: the control
renames ``raw_row`` in the published message and the writer refuses it. The real-broker reading of this seam is the
row read back from ``work_ledger_rows`` after the emit ran on a lab host (the emit guard refuses a test process any
real broker, so no test publishes there).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.events.enum_ledger_row_type import EnumLedgerRowType
from omnimarket.models.model_work_ledger_fold_request import ModelWorkLedgerFoldRequest
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.handler_work_ledger_emit import (
    HandlerWorkLedgerEmit,
)
from omnimarket.nodes.node_work_ledger_emit_effect.models.model_work_ledger_emit_request import (
    ModelWorkLedgerEmitRequest,
)
from tests.helpers.work_ledger_open_gate import OPEN_JUDGE_GATE
from tests.unit.nodes.node_projection_work_ledger.test_work_ledger_writer import (
    _FakeDb,
    _writer,
)
from tests.unit.nodes.node_work_ledger_emit_effect.declared_broker_support import (
    OpenInmemoryBus,
    install_emit_lane,
    route_declared_bus,
)

pytestmark = pytest.mark.unit

ROW_ID, ROW_TYPE, RAW_ROW = (
    0,
    3,
    6,
)  # argument positions of the projector's work_ledger_rows insert
REQUIRED_BY_PROJECTOR = {
    name
    for name, field in ModelWorkLedgerFoldRequest.model_fields.items()
    if field.is_required()
}


async def _emit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, row: str
) -> tuple[Any, dict[str, Any]]:
    install_emit_lane(tmp_path, monkeypatch, {"type": "kafka", "lane": "dev"})
    bus = OpenInmemoryBus()
    route_declared_bus(monkeypatch, bus)
    result = HandlerWorkLedgerEmit(judge_gate=OPEN_JUDGE_GATE).handle(
        ModelWorkLedgerEmitRequest(row=row)
    )
    assert result.accepted is True
    assert result.topic is not None
    published = await bus.published(result.topic)
    assert len(published) == 1
    return result, published[0]


@pytest.mark.parametrize("row_type", list(EnumLedgerRowType), ids=lambda t: t.value)
async def test_the_projector_writes_the_row_the_emit_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rows: dict[str, str],
    row_type: EnumLedgerRowType,
) -> None:
    result, message = await _emit(tmp_path, monkeypatch, rows[row_type.value])
    assert set(message) >= REQUIRED_BY_PROJECTOR, (
        "the emit envelope lost a field the projector requires"
    )

    db = _FakeDb()
    written = _writer(db).handle({**message, "_topic": row_type.topic})

    assert written["rows_upserted"] >= 1
    args = db.statements[0][1]
    assert args[ROW_ID] == result.row_id == message["row_id"]
    assert args[ROW_TYPE] == row_type.value
    assert args[RAW_ROW] == rows[row_type.value]


async def test_a_renamed_envelope_field_is_refused_by_the_projector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: dict[str, str]
) -> None:
    _, message = await _emit(tmp_path, monkeypatch, rows["CLAIM"])
    renamed = {
        ("row_text" if key == "raw_row" else key): value
        for key, value in message.items()
    }
    db = _FakeDb()

    with pytest.raises(Exception, match="raw_row"):
        _writer(db).handle({**renamed, "_topic": EnumLedgerRowType.CLAIM.topic})

    assert db.statements == []
