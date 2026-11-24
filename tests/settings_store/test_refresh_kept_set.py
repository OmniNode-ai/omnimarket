# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC7 (Rule 6, finding 2 of the capped review): after an overlay's cutover no host refresh keeps or writes a copy of it."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers.overlay_store import kept_set
from omnimarket.handlers.overlay_store.kept_set import (
    KeptSetCutOverError,
    require_no_cut_over_overlay,
)


def test_refresh_kept_set_a_cut_over_overlay_in_the_kept_set_refuses_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        kept_set, "CUT_OVER_OVERLAYS", frozenset({"node_floor_fill_orchestrator"})
    )
    with pytest.raises(KeptSetCutOverError, match="node_floor_fill_orchestrator"):
        require_no_cut_over_overlay(["lab_hosts", "node_floor_fill_orchestrator"])


def test_refresh_kept_set_an_overlay_added_after_cutover_is_refused_the_same_way(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        kept_set, "CUT_OVER_OVERLAYS", frozenset({"node_floor_fill_orchestrator"})
    )
    with pytest.raises(KeptSetCutOverError):
        require_no_cut_over_overlay(["node_floor_fill_orchestrator"])


def test_refresh_kept_set_positive_control_other_overlays_stay_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        kept_set, "CUT_OVER_OVERLAYS", frozenset({"node_floor_fill_orchestrator"})
    )
    require_no_cut_over_overlay(["lab_hosts", "llm_endpoints"])


async def test_refresh_kept_set_the_refresh_handler_writes_no_snapshot_for_a_cut_over_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from datetime import UTC, datetime

    from omnimarket.handlers.overlay_store.snapshot_root import FileOverlaySnapshot
    from omnimarket.handlers.overlay_store.store_in_memory import InMemoryOverlayStore
    from omnimarket.models.overlay_store import ModelOverlayStoreRecord, document_digest
    from omnimarket.nodes.node_overlay_store_read_effect.handlers import (
        HandlerOverlayStoreRead,
    )

    monkeypatch.setattr(
        kept_set, "CUT_OVER_OVERLAYS", frozenset({"node_floor_fill_orchestrator"})
    )
    document = {"floor_fill": {"sent_window_minutes": 30}}
    store = InMemoryOverlayStore()
    await store.write_version(
        ModelOverlayStoreRecord(
            overlay_id="node_floor_fill_orchestrator",
            digest=document_digest(document),
            document=document,
            source_ref="ref",
            loaded_by="t",
            loaded_at=datetime(2026, 11, 24, tzinfo=UTC),
        ).model_dump(mode="json")
    )

    class _Reporter:
        async def report(self, reading: object) -> None:
            return None

    root = tmp_path / "snap"
    handler = HandlerOverlayStoreRead(
        store=store,
        snapshot=FileOverlaySnapshot(root),
        reporter=_Reporter(),
        host="h",
        policies={},
    )
    with pytest.raises(KeptSetCutOverError):
        await handler.handle("node_floor_fill_orchestrator")
    assert list(root.rglob("*")) == []
