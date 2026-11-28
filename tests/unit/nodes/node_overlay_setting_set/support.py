# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A live record of the hotload overlay, a bus that records what it is given, and the handler wired as the contract declares."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Any

import yaml
from pydantic import JsonValue

from omnimarket.models.overlay_store import ModelOverlayStoreRecord, document_digest
from omnimarket.nodes.node_overlay_setting_set_effect.handlers.handler_overlay_setting_set import (
    HandlerOverlaySettingSet,
)
from tests.unit.nodes.node_projection_overlay_store.support import Loopback

NODE = "node_overlay_setting_set_effect"
OVERLAY_ID = "runtime_contract_hotload"
KEY = "lanes.lane-a.lazy_start"
PATH = "lanes/lane-a/lazy_start"
ACTOR = "storeset-0315"
NOW = datetime(2026, 11, 23, 4, 0, tzinfo=UTC)
LIVE_LOADED_AT = NOW - timedelta(minutes=20)

OLD_LAZY_START: dict[str, JsonValue] = {
    "ingress_contract": "node_old_ingress",
    "budget_seconds": 60.0,
    "max_retries": 1,
    "boot_contracts": ["node_a"],
}
BOOT_SET: dict[str, JsonValue] = {
    "ingress_contract": "node_lazy_start_ingress",
    "budget_seconds": 30,
    "max_retries": 3,
    "boot_contracts": [f"node_boot_{n:02d}" for n in range(53)],
}


def contract() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    loaded: dict[str, Any] = yaml.safe_load(text)
    return loaded


def hotload_document() -> dict[str, JsonValue]:
    return {
        "schema_version": 1,
        "lanes": {
            "lane-a": {
                "enabled": True,
                "mode": "on_demand",
                "allowed_environments": ["dev"],
                "lazy_start": copy.deepcopy(OLD_LAZY_START),
            },
            "lane-b": {
                "enabled": True,
                "mode": "full_load",
                "allowed_environments": ["stage"],
            },
        },
    }


def live_record(
    document: Mapping[str, JsonValue] | None = None,
    *,
    loaded_at: datetime = LIVE_LOADED_AT,
    operator_paths: tuple[str, ...] = (),
    source_ref: str = "runtime_contract_hotload@abc123",
) -> dict[str, Any]:
    doc = dict(hotload_document() if document is None else document)
    base = ModelOverlayStoreRecord(
        overlay_id=OVERLAY_ID,
        digest=document_digest(doc),
        document=doc,
        source_ref=source_ref,
        loaded_by="loader",
        loaded_at=loaded_at,
    ).model_dump(mode="json")
    if operator_paths:
        base["operator_paths"] = list(operator_paths)
    return base


class SetBus:
    """What reached the broker on the two topics a set leaves: its write on the loaded topic, then the audit row the
    projection publishes from the table. ``(topic, key, value)`` in order."""

    def __init__(self, loop: Loopback) -> None:
        self._loop = loop

    @property
    def published(self) -> list[tuple[str, bytes | None, bytes]]:
        return list(self._loop.sequence)

    def on(self, topic: str) -> list[tuple[bytes | None, bytes]]:
        return [(k, v) for t, k, v in self._loop.sequence if t == topic]


def build(
    live: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    max_live_age_s: float = 7200.0,
) -> tuple[HandlerOverlaySettingSet, SetBus, Loopback]:
    """The handler wired as the contract declares, over the loopback broker and projection, the store as it is read
    from the snapshot topic."""
    loop = Loopback()
    for record in ({OVERLAY_ID: live_record()} if live is None else live).values():
        loop.seed_snapshot(record)
    handler = loop.handler(max_live_age_s=max_live_age_s, clock=lambda: NOW)
    return handler, SetBus(loop), loop
