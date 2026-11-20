# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A stored zero that no price stood behind is served as unknown, never as $0.

An older terminal shape stored cost_usd 0 for every run it could not price. On an
operator store (2026-11-20, 4,600 zero-cost runs) the attempt records carry no price
evidence at all: price_source and cost_basis are empty everywhere, and usage_source
reads "measured" both for unpriced Beam runs and for free local runs. What does tell
them apart is what makes a run free: a local tier, a ":free" model id, or a price the
pricing manifest declares (zero included). A zero with none of those is unknown.
"""

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from omnimarket.models.model_projection_read import ModelProjectionReadRequest
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from tests.unit.projection.test_sqlite_delegation_routing_quality_views_omn20754 import (
    _TOPICS,
)
from tests.unit.projection.test_sqlite_delegation_savings_view_omn20008 import (
    _TENANT,
    _delegation,
    _store,
)

pytestmark = pytest.mark.unit

_MEASURED = [{"usage_source": "measured", "cost_usd": 0.0}]
_UNKNOWN = [{"usage_source": "unknown", "cost_usd": 0.0}]
_SAVED = "0.045068"

# key: (tier, model, stored cost, recorded cost source, attempts, served as unknown?)
_CASES: dict[
    str,
    tuple[
        str | None, str | None, float | None, str | None, list[dict[str, object]], bool
    ],
] = {
    # Unpriced cloud model; usage was measured, the price never was.
    "beam-measured-usage": (
        "cheap_cloud",
        "Beam-1.0-501B-A23B",
        0.0,
        None,
        _MEASURED,
        True,
    ),
    "beam-unknown-usage": (
        "cheap_cloud",
        "Beam-1.0-501B-A23B",
        0.0,
        None,
        _UNKNOWN,
        True,
    ),
    "frontier-unpriced": (
        "cheap_frontier",
        "gemini-3.5-flash-lite",
        0.0,
        None,
        _MEASURED,
        True,
    ),
    # Free: a local tier, whatever its attempts say.
    "local-tier": ("local", "qwen3.5:0.8b", 0.0, None, _UNKNOWN, False),
    # Free: a ":free" model id on a cloud tier.
    "free-id": (
        "cheap_frontier",
        "thinkingmachines/inkling:free",
        0.0,
        None,
        _UNKNOWN,
        False,
    ),
    # Free: the manifest declares a zero price (no tier recorded).
    # A tier or :free suffix written in another case or with spaces is still free.
    "local-tier-spelling": (" Local ", "qwen3.5:0.8b", 0.0, None, _UNKNOWN, False),
    "free-id-upper-case": (
        "cheap_frontier",
        "vendor/model:FREE",
        0.0,
        None,
        _UNKNOWN,
        False,
    ),
    # No tier recorded: nothing shows the run was cloud-priced, so it is served as stored.
    "no-tier-unpriced-model": (None, "Beam-1.0-501B-A23B", 0.0, None, _MEASURED, False),
    "declared-zero-price": (None, "Qwen3.8-27B", 0.0, None, _UNKNOWN, False),
    # Charged runs and explicitly recorded sources are served as stored.
    "charged": ("cheap_cloud", "Beam-1.0-501B-A23B", 1.5, None, _MEASURED, False),
    "recorded-source": (
        "cheap_cloud",
        "Beam-1.0-501B-A23B",
        0.0,
        "free_local",
        _UNKNOWN,
        False,
    ),
    # Nothing names the model: nothing to decide on, served as stored.
    "no-model": ("cheap_cloud", None, 0.0, None, _UNKNOWN, False),
    "no-amount": ("cheap_cloud", "Beam-1.0-501B-A23B", None, None, _UNKNOWN, False),
}


def _read(path: Path) -> list[dict[str, object]]:
    topic = "onex.snapshot.projection.delegation.decisions.v1"
    result = asyncio.run(
        HandlerProjectionRead(
            topic_map=_TOPICS, row_source=SqliteTableRowSource(path)
        ).handle(ModelProjectionReadRequest(topic=topic, tenant_id=_TENANT))
    )
    assert result.ok, result.response
    return list(result.rows)


def test_a_zero_is_unknown_only_when_nothing_made_the_run_free_or_priced(
    tmp_path: Path,
) -> None:
    path = _store(
        tmp_path,
        tuple(_delegation(key, cost_tier_name="cheap_cloud") for key in _CASES),
    )
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE delegation_events ADD COLUMN attempt_history")
        db.execute("ALTER TABLE delegation_events ADD COLUMN cost_measurement_source")
        for key, (tier, model, cost, source, attempts, _) in _CASES.items():
            db.execute(
                "UPDATE delegation_events SET cost_tier_name=?, model_name=?, delegated_to=?, "
                "cost_usd=?, cost_measurement_source=?, attempt_history=?, "
                "modelled_savings_usd=?, savings_measurement='measured' WHERE correlation_id=?",
                (tier, model, model, cost, source, json.dumps(attempts), _SAVED, key),
            )

    rows = _read(path)

    assert all("attempt_history" not in row for row in rows)
    served = {
        str(r["correlation_id"]): (
            r["cost_usd"],
            r["modelled_savings_usd"],
            r["savings_measurement"],
        )
        for r in rows
    }
    expected = {
        key: (None, None, "unknown_spend") if unknown else (cost, _SAVED, "measured")
        for key, (_, _, cost, _, _, unknown) in _CASES.items()
    }
    assert served == expected
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT cost_usd, modelled_savings_usd, savings_measurement "
            "FROM delegation_events WHERE correlation_id='beam-measured-usage'"
        ).fetchone() == (0.0, _SAVED, "measured")


def test_a_store_without_tier_or_source_columns_is_served_as_stored(
    tmp_path: Path,
) -> None:
    # The oldest stores predate cost_measurement_source and attempt_history; a store
    # without the columns the decision reads must still serve every row, unchanged.
    path = _store(tmp_path, (_delegation("old", cost_tier_name="local"),))
    rows = _read(path)
    assert [str(r["correlation_id"]) for r in rows] == ["old"]
    assert rows[0]["cost_usd"] == 0.0
