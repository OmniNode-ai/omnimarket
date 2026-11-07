# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures: three copies of a synthetic host table, in memory or as files named by an overlay.

A test breaks one thing by name: a cap changed in one copy, a field present in one copy only, a host
missing from one copy, an unreadable copy.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_host_table_copies_drift_compute.adapters.copy_source import (
    InMemoryCopySource,
)
from omnimarket.nodes.node_host_table_copies_drift_compute.handlers.handler_host_table_copies_drift import (
    HandlerHostTableCopiesDrift,
)
from omnimarket.nodes.node_host_table_copies_drift_compute.overlay import (
    ModelHostTableCopiesDriftOverlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)

NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_host_table_copies_drift_compute"
)
EXAMPLE = NODE / "overlay.example.yaml"
RUN_AT = datetime(2026, 11, 7, 6, 40, tzinfo=UTC)
HOST = "example-host"
COPIES = ("packaged", "config", "overlays")

BASE_HOSTS: list[dict[str, Any]] = [
    {
        "name": "host-a",
        "target": "host-a.example.test",
        "cap": 3,
        "docker": True,
        "clones": [{"repo": "x/y", "ref": "dev"}],
    },
    {"name": "host-b", "target": "host-b.example.test", "cap": 6, "available": False},
]


def overlay_document(paths: dict[str, str] | None = None) -> dict[str, Any]:
    """The shipped example overlay with the three copies of this test and, when given, their paths."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["copies"] = [
        {"name": name, "path": (paths or {}).get(name, f"$EXAMPLE_COPY_{name.upper()}")}
        for name in COPIES
    ]
    return document


def overlay() -> ModelHostTableCopiesDriftOverlay:
    return ModelHostTableCopiesDriftOverlay.model_validate(overlay_document())


def table_text(hosts: list[dict[str, Any]]) -> str:
    return yaml.safe_dump({"hosts": hosts}, sort_keys=False)


class World:
    """Three identical copies until a test breaks one."""

    def __init__(self) -> None:
        self.hosts = {name: copy.deepcopy(BASE_HOSTS) for name in COPIES}
        self.source_errors: dict[str, str] = {}
        self.raw: dict[str, str] = {}

    # Named breaks -------------------------------------------------------------------------
    def break_change_a_cap(
        self, copy: str = "config", host: str = "host-a", cap: int = 6
    ) -> None:
        for row in self.hosts[copy]:
            if row["name"] == host:
                row["cap"] = cap

    def break_add_a_field_to_one_copy(self, copy: str = "overlays") -> None:
        self.hosts[copy][1]["unit_slots"] = 2

    def break_drop_a_host_from_one_copy(self, copy: str = "config") -> None:
        self.hosts[copy] = [row for row in self.hosts[copy] if row["name"] != "host-b"]

    def break_make_a_copy_unreadable(self, copy: str = "overlays") -> None:
        self.source_errors[copy] = "file-missing"

    def break_make_a_copy_not_a_table(self, copy: str = "config") -> None:
        self.raw[copy] = "- not\n- a table\n"

    # Backends ---------------------------------------------------------------------------
    def texts(self) -> dict[str, str]:
        return {
            name: self.raw.get(name, table_text(self.hosts[name])) for name in COPIES
        }

    def source(self) -> InMemoryCopySource:
        source = InMemoryCopySource(self.texts())
        source.read_errors = dict(self.source_errors)
        return source

    def handler(self, ledger: InMemoryLedger | None) -> HandlerHostTableCopiesDrift:
        return HandlerHostTableCopiesDrift(
            overlay=overlay(),
            host=HOST,
            source=self.source(),
            appender=ledger,
            clock=lambda: RUN_AT,
        )
