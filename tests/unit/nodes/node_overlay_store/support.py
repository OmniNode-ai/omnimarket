# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A table shaped like the real one, an overlay home on disk, and stores that break by name."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml

from omnimarket.handlers.overlay_store.snapshot_root import FileOverlaySnapshot
from omnimarket.models.overlay_store import (
    ModelOverlayReadPolicy,
    ModelOverlayStoreReading,
    ModelOverlayStoreRecord,
    document_digest,
)
from omnimarket.nodes.node_overlay_store_read_effect.handlers import (
    HandlerOverlayStoreRead,
)

NOW = datetime(2026, 11, 6, 1, 0, tzinfo=UTC)
BOUND = 3600


def contract_topics(node: str, key: str) -> list[str]:
    """The topics a node's contract declares under ``event_bus.<key>``."""
    text = (
        resources.files(f"omnimarket.nodes.{node}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    topics: list[str] = yaml.safe_load(text)["event_bus"][key]
    return topics


LOADED_TOPIC = contract_topics("node_overlay_store_load_effect", "publish_topics")[0]
SNAPSHOT_TOPIC = contract_topics("node_projection_overlay_store", "publish_topics")[0]

TABLE: dict[str, Any] = {
    "hosts": [
        {
            "name": "h1",
            "target": "h1.example.invalid",
            "fallback_targets": ["alias-1"],
            "fallback_ssh_user": "lab",
            "fallback_proxy_jump": "jump-host",
            "ssh_user": "lab",
            "cap": 12,
            "penalty": 0.25,
            "tenant_id": "00000000-0000-0000-0000-000000000000",
            "ledger_path": "$OMNI_HOME/docs/ledger.md",
            "ledger_bus_lane": "test-bus",
            "ledger_bus_python": ".venv/bin/python",
            "clones": [{"repo": "o/r", "ref": "dev", "path": "$OMNI_HOME/r"}],
        },
        {
            "name": "h2",
            "target": "h2.example.invalid",
            "available": False,
            "unavailable_reason": "its owner's x: "
            + "a long sentence with a colon, " * 12,
            "cap": 3,
        },
    ],
    "unit_needs": [],
    "registry_root_candidates": ["$HOME/registry"],
    "bus_lane": "test-bus",
}

PINS = """\
# one line per overlay
lab_hosts 0123456789 omnimarket.models.overlay_keys:ModelLabHostsOverlay
outside 0123456789 os.path:join
"""


def overlay_home(tmp_path: Path, table: Mapping[str, Any] = TABLE) -> Path:
    clone = tmp_path / "home"
    (clone / "overlays" / "lab_hosts").mkdir(parents=True)
    (clone / "overlays" / "lab_hosts" / "overlay.yaml").write_text(
        yaml.safe_dump(dict(table), sort_keys=False), encoding="utf-8"
    )
    (clone / ".omnimarket-node-pins").write_text(PINS, encoding="utf-8")
    return clone


def record(
    table: Mapping[str, Any] = TABLE, *, loaded_at: datetime = NOW
) -> ModelOverlayStoreRecord:
    document = dict(table)
    return ModelOverlayStoreRecord(
        overlay_id="lab_hosts",
        digest=document_digest(document),
        document=document,
        source_ref="lab_hosts@abc",
        loaded_by="test",
        loaded_at=loaded_at,
    )


class FakeBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes | None, bytes]] = []

    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: Any = None
    ) -> None:
        self.published.append((topic, key, value))


class Store:
    """Answers with a record, nothing, or a raise, as the case names."""

    def __init__(
        self, answer: Mapping[str, Any] | None = None, *, down: bool = False
    ) -> None:
        self._answer = answer
        self._down = down

    async def write_version(self, record_: Mapping[str, Any]) -> bool:
        return True

    async def read_current(self, overlay_id: str) -> Mapping[str, Any] | None:
        if self._down:
            raise ConnectionError("broker refused")
        return self._answer


class Reporter:
    def __init__(self, *, fail: bool = False) -> None:
        self.readings: list[ModelOverlayStoreReading] = []
        self._fail = fail

    async def report(self, reading: ModelOverlayStoreReading) -> None:
        if self._fail:
            raise ConnectionError("no broker to report on")
        self.readings.append(reading)


def reader(
    tmp_path: Path,
    store: Any,
    *,
    reporter: Reporter | None = None,
    now: datetime = NOW,
    on_stale: Literal["read", "refuse"] = "read",
) -> HandlerOverlayStoreRead:
    return HandlerOverlayStoreRead(
        store=store,
        snapshot=FileOverlaySnapshot(tmp_path / "snapshot"),
        reporter=reporter or Reporter(),
        host="h1",
        policies={
            "lab_hosts": ModelOverlayReadPolicy(stale_after_s=BOUND, on_stale=on_stale)
        },
        clock=lambda: now,
    )


def past_bound() -> datetime:
    return NOW + timedelta(seconds=BOUND + 1)


__all__ = [
    "BOUND",
    "LOADED_TOPIC",
    "NOW",
    "PINS",
    "SNAPSHOT_TOPIC",
    "TABLE",
    "FakeBus",
    "Reporter",
    "Store",
    "overlay_home",
    "past_bound",
    "reader",
    "record",
]
