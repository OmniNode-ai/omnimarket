# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Captured records -> install-confirm compute -> compaction receipts writer."""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.context_install_confirm import ModelInstallConfirmRequest
from omnimarket.nodes.node_context_install_confirm_compute.handlers.handler_context_install_confirm import (
    HandlerContextInstallConfirm,
)
from omnimarket.nodes.node_projection_compaction_receipts.handlers.handler_projection_compaction_receipts import (
    HandlerProjectionCompactionReceipts,
)
from tests.test_compaction_receipts_projection_writer import (
    FIXTURES,
    NODE,
    _Db,
    _writer,
)

pytestmark = pytest.mark.unit


def test_golden_chain_compaction_receipts_projection_writer_records_computed_verdicts() -> (
    None
):
    request = ModelInstallConfirmRequest.model_validate_json(
        (FIXTURES / "install_confirm_requested.json").read_text()
    )
    computed = HandlerContextInstallConfirm().handle(request)
    writer = _writer(_Db())
    assert isinstance(writer._fold, HandlerProjectionCompactionReceipts)
    result = writer.handle(computed.model_dump(mode="json"))
    assert result["rows_upserted"] == 5
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.projection-compaction-receipts-applied.v1"
    )
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    snapshot_topics = {exposure.topic for (exposure,), _ in writer.snapshots}
    assert snapshot_topics == {"onex.snapshot.projection.compaction-receipts.v1"}
    assert snapshot_topics <= set(contract["event_bus"]["publish_topics"])
    for boundary, fact in zip(computed.boundaries[:2], computed.facts[:2], strict=True):
        (row,) = [
            row
            for row in result["compaction_receipts_rows"]
            if row["boundary_uuid"] == boundary.boundary_uuid
        ]
        assert row["verdict"] == fact.fact.value
        assert row["activation_id"] == str(fact.activation_id)


def test_golden_chain_compaction_receipts_projection_writer_refuses_malformed_result() -> (
    None
):
    db = _Db()
    writer = _writer(db)
    with pytest.raises(ValidationError):
        writer.handle({"session_id": "malformed", "unexpected": "refused"})
    assert db.calls == []
    assert writer.snapshots == []
