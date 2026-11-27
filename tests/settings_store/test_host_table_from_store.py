# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Task 20: the board's lab_hosts reader reads the host table through the injected settings store and nothing else."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omnimarket.handlers.overlay_store.store_in_memory import InMemoryOverlayStore
from omnimarket.nodes.node_host_table_keys_read_effect.handlers.reader_host_table import (
    read_host_table_rows_from_store,
)

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
BOUND = 900.0
READER = Path(
    "src/omnimarket/nodes/node_host_table_keys_read_effect/handlers/reader_host_table.py"
)


class _Memory(InMemoryOverlayStore):
    async def write_version(self, record: Any) -> bool:
        self._records[str(record["overlay_id"])] = dict(record)
        return True


def _document() -> dict[str, Any]:
    return {
        "hosts": [
            {"name": "alpha", "cap": 4, "available": True, "target": "alpha"},
            {
                "name": "beta",
                "cap": 2,
                "target": "beta",
                "available": False,
                "unavailable_reason": "disk full",
            },
        ],
        "registry_root_candidates": ["$HOME/registry"],
    }


async def _store(beat: datetime = NOW) -> _Memory:
    store = _Memory()
    await store.write_version(
        {
            "overlay_id": "lab_hosts",
            "revision": 3,
            "heartbeat_at": beat.isoformat(),
            "document": _document(),
        }
    )
    return store


async def test_rows_come_from_the_injected_store() -> None:
    rows = await read_host_table_rows_from_store(
        await _store(), now=NOW, max_staleness_seconds=BOUND
    )
    assert rows is not None
    assert [(r.name, r.available, r.exclusion_reason) for r in rows] == [
        ("alpha", True, ""),
        ("beta", False, "disk full"),
    ]


async def test_an_absent_overlay_reads_unknown_never_an_empty_list() -> None:
    assert (
        await read_host_table_rows_from_store(
            _Memory(), now=NOW, max_staleness_seconds=BOUND
        )
        is None
    )


async def test_a_stale_heartbeat_reads_unknown() -> None:
    old = NOW - timedelta(seconds=BOUND + 1)
    assert (
        await read_host_table_rows_from_store(
            await _store(old), now=NOW, max_staleness_seconds=BOUND
        )
        is None
    )


async def test_an_unreachable_store_reads_unknown() -> None:
    class _Closed(_Memory):
        async def read_current(self, overlay_id: str) -> Any:
            raise ConnectionRefusedError("closed port")

    assert (
        await read_host_table_rows_from_store(
            _Closed(), now=NOW, max_staleness_seconds=BOUND
        )
        is None
    )


def test_the_reader_names_no_implementation_and_no_environment() -> None:
    tree = ast.parse(READER.read_text())
    imported = " ".join(
        n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
    )
    assert "store_host_keys" not in imported
    assert (
        "environ"
        not in READER.read_text().split("def read_host_table_rows_from_store")[1]
    )
