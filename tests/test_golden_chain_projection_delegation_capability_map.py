# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20169 (RS.5): the capability map writer, its contract and the labelled-item seed."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml

from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.delegation_capability_map.enum_capability_inadmissible_reason import (
    EnumCapabilityInadmissibleReason,
)
from omnimarket.nodes.node_projection_delegation_capability_map.handlers import (
    HandlerDelegationCapabilityMapWriter,
    HandlerProjectionDelegationCapabilityMap,
)
from omnimarket.nodes.node_projection_delegation_capability_map.lab_run import (
    build_map_run,
)
from omnimarket.projection.runner import BaseProjectionRunner

pytestmark = pytest.mark.unit

_IN_TOPIC = "onex.evt.omnimarket.delegation-capability-map-run-recorded.v1"  # onex-topic-allow: asserted against the contract
_OUT_TOPIC = "onex.evt.omnimarket.projection-delegation-capability-map-applied.v1"  # onex-topic-allow: asserted against the contract
_DLQ_TOPIC = "onex.dlq.omnimarket.projection-delegation-capability-map-malformed.v1"  # onex-topic-allow: asserted against the contract
_NODE = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_delegation_capability_map"
)
_TENANT = UUID("11111111-1111-1111-1111-111111111111")
_T0 = datetime(2026, 10, 1, tzinfo=UTC)
_KEY = (
    "tenant_id",
    "map_run_id",
    "model_key",
    "backend_id",
    "configuration_id",
    "task_class",
    "size_band",
)


def _items(
    n: int, adequate: int, *, task_class: str = "code_review", **extra: Any
) -> list[dict[str, Any]]:
    return [
        {
            "item_key": f"{task_class}-{i}:0",
            "task_class": task_class,
            "label": "adequate" if i < adequate else "inadequate",
            "prompt_snapshot": f"Review this hunk {i}: def f(x): return x + 1",
            "observed_at": _T0 + timedelta(minutes=i),
            "rubric_version": "ev4-blind-v1",
            "rater_role": "blind_model:claude-opus-5-5",
            "model_key": "qwen3-coder-30b",
            "backend_id": "local-url",
        }
        | extra
        for i in range(n)
    ]


class _Store:
    """Loop-bound adapter double that keeps the table's conflict key and guard."""

    def __init__(self) -> None:
        self.rows: dict[tuple[Any, ...], dict[str, Any]] = {}
        self.loop: asyncio.AbstractEventLoop | None = None

    async def connect(self) -> None:
        assert self.loop is None
        self.loop = asyncio.get_running_loop()

    async def close(self) -> None:
        self.loop = None

    async def execute(
        self, query: str, *params: Any, tenant: str | None = None
    ) -> list[dict[str, Any]]:
        assert self.loop is asyncio.get_running_loop()
        assert (
            "ON CONFLICT (tenant_id, map_run_id, model_key, backend_id, configuration_id,"
            in query
        )
        assert "observed_at < EXCLUDED.observed_at" in query
        columns = query.split("(", 1)[1].split(")", 1)[0].replace("\n", "").split(",")
        row = dict(zip([c.strip() for c in columns], params, strict=True))
        assert isinstance(row["tenant_id"], UUID)
        assert tenant == str(row["tenant_id"])
        assert isinstance(row["observed_at"], datetime)
        json.loads(row["configuration_parameters"])
        key = tuple(row[c] for c in _KEY)
        if key in self.rows and self.rows[key]["observed_at"] >= row["observed_at"]:
            return []
        self.rows[key] = row
        return [{"map_run_id": row["map_run_id"]}]


def _writer() -> tuple[HandlerDelegationCapabilityMapWriter, _Store]:
    writer = HandlerDelegationCapabilityMapWriter()
    store = _Store()
    writer.__dict__["_db"] = store
    return writer, store


def _run_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
    run, _ = build_map_run(_TENANT, items)
    return run.model_dump(mode="json")


def test_the_writer_declares_in_process_dispatch() -> None:
    assert issubclass(HandlerDelegationCapabilityMapWriter, BaseProjectionRunner)
    assert HandlerDelegationCapabilityMapWriter.onex_runtime_inprocess_dispatch is True
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    # Only the writer is routed; it calls the pure fold in process (rule 7a).
    assert {h["handler"]["name"] for h in contract["handler_routing"]["handlers"]} == {
        "HandlerDelegationCapabilityMapWriter"
    }
    writer, _ = _writer()
    assert isinstance(writer._derive, HandlerProjectionDelegationCapabilityMap)


def test_the_contract_declares_the_topics_and_the_cells_table() -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    assert contract["event_bus"] == {
        "subscribe_topics": [_IN_TOPIC],
        "publish_topics": [_OUT_TOPIC],
        "dlq_topics": [_DLQ_TOPIC],
    }
    assert contract["terminal_event"] == _OUT_TOPIC
    assert _writer()[0].subscribe_topics == [_IN_TOPIC]
    (table,) = contract["db_io"]["db_tables"]
    assert (table["schema"], table["name"], table["access"]) == (
        "public",
        "delegation_capability_cells",
        "read_write",
    )
    migration = (_NODE / "migrations" / table["migration"]).read_text()
    assert (
        "PRIMARY KEY (tenant_id, map_run_id, model_key, backend_id, configuration_id,"
        in migration
    )
    assert "ENABLE ROW LEVEL SECURITY" in migration


def test_a_map_run_writes_one_row_per_cell() -> None:
    writer, store = _writer()
    payload = _run_payload(_items(30, 25) + _items(12, 4, task_class="document"))
    result = writer.handle(payload)
    assert result["rows_upserted"] == result["cells"] == len(store.rows) >= 2
    assert result["rows_handled"] == result["rows_upserted"]
    assert {(k[5], k[6]) for k in store.rows} <= {
        (c, b.value) for c in ("code_review", "document") for b in EnumSizeBand
    }
    by_class = {}
    for row in store.rows.values():
        by_class.setdefault(row["task_class"], 0)
        by_class[row["task_class"]] += row["n"]
    assert by_class == {"code_review": 30, "document": 12}
    assert all(row["verdict"] == "REFUSED" for row in store.rows.values())
    assert store.loop is None


def test_a_redelivered_run_writes_no_row_and_still_counts_as_handled() -> None:
    writer, store = _writer()
    payload = _run_payload(_items(20, 20))
    first = writer.handle(payload)
    again = writer.handle(payload)
    assert first["rows_upserted"] >= 1
    assert again["rows_upserted"] == 0
    assert again["rows_refused_by_ordering_guard"] == first["rows_upserted"]
    assert again["rows_handled"] == first["rows_handled"]
    assert len(store.rows) == first["rows_upserted"]


def test_the_labelled_seed_refuses_items_with_no_recorded_backend() -> None:
    items = _items(10, 10) + [
        item | {"item_key": f"x-{i}", "backend_id": None}
        for i, item in enumerate(_items(4, 4))
    ]
    run, skipped = build_map_run(_TENANT, items)
    assert skipped == {}
    writer, store = _writer()
    result = writer.handle(run.model_dump(mode="json"))
    assert result["folded_rows"] == 10
    assert result["inadmissible"] == {
        EnumCapabilityInadmissibleReason.IDENTITY_FIELD_MISSING.value: 4
    }
    assert sum(row["n"] for row in store.rows.values()) == 10


def test_the_labelled_seed_is_one_run_per_label_set() -> None:
    items = _items(8, 5)
    first, _ = build_map_run(_TENANT, items)
    again, _ = build_map_run(_TENANT, list(reversed(items)))
    assert first.map_run_id == again.map_run_id
    relabelled, _ = build_map_run(_TENANT, _items(8, 6))
    assert relabelled.map_run_id != first.map_run_id
    _, skipped = build_map_run(
        _TENANT, [*items, items[0] | {"item_key": "u", "label": "unlabelable"}]
    )
    assert skipped == {"label_not_adequate_or_inadequate": 1}
