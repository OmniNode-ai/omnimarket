# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A storage seam returning a final row must not leak its pre-update proposal.

This tests transport ownership only: no store or monetary computation is
mocked into evidence of financial correctness. Real-store races cover those.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.runner import MessageMeta
from tests.test_omn18140_delegation_writer_attestation import _mock_db, _runner

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("path", ["async", "sync"])
async def test_only_the_final_post_update_row_is_published(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal: dict[str, object] = {
        "correlation_id": "publication-seam-control",
        "modelled_savings_usd": None,
    }
    final: dict[str, object] = {
        **proposal,
        "modelled_savings_usd": "0.010",
        "savings_baseline_model": "returned-baseline",
        "savings_pricing_manifest_version": "returned-manifest",
        "savings_measurement": "measured",
    }
    if path == "async":
        runner = _runner(_mock_db())
        write = AsyncMock(return_value=[final])
        publish = AsyncMock()
        monkeypatch.setattr(runner, "_dynamic_upsert", write)
        monkeypatch.setattr(runner, "_publish_row_snapshot", publish)
        await runner._write_delegation_row(
            proposal, MessageMeta(partition=0, offset=0, fallback_id="seam")
        )
    else:
        handler = HandlerProjectionDelegation()

        class ReturnedRowAdapter(InmemoryDatabaseAdapter):
            def upsert_returning(
                self, *args: object, **kwargs: object
            ) -> list[dict[str, object]]:
                return [final]

        db = ReturnedRowAdapter()
        publish = MagicMock()
        monkeypatch.setattr(handler, "_publish_row_snapshot", publish)
        monkeypatch.setattr(handler, "_publish_aggregate_snapshots", MagicMock())
        handler._write_delegation_row(db, proposal)
    assert publish.call_args.args[0] == [final]
