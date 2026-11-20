# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A legacy unpriced zero is served as unknown without modifying the recorded charge."""

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


def test_serve_only_a_zero_known_to_have_been_unpriced_as_unknown(
    tmp_path: Path,
) -> None:
    # The historical unpriced call and today's measured local/declared-free call
    # both used zero as their numeric amount. Neither today's catalog nor the
    # provider/model name can tell which amount was known at call time.
    unknown = {"usage_source": "unknown", "cost_usd": 0.0}
    measured = {"usage_source": "measured", "cost_usd": 0.0}
    examples = {
        "unpriced": ([unknown], 0.0, None),
        "declared-free": ([measured], 0.0, None),
        "mixed": ([unknown, measured], 0.0, None),
        "old-no-provenance": ([{}], 0.0, None),
        "incomplete": ([unknown, {}], 0.0, None),
        "attempt-recorded-charge": ([{**unknown, "cost_usd": 0.04}], 0.0, None),
        "unrecognized-shape": (["unknown"], 0.0, None),
        "malformed-zero": ([{**unknown, "cost_usd": False}], 0.0, None),
        "damaged-history": ("invalid JSON", 0.0, None),
        "deeply-nested-history": ("[" * 2000 + "]" * 2000, 0.0, None),
        "explicit-attempt-price": (
            [{**unknown, "price_source": "models.dev"}],
            0.0,
            None,
        ),
        "explicit-free-basis": (
            [{**unknown, "cost_basis": "zero_marginal_api_cost"}],
            0.0,
            None,
        ),
        "no-attempts": ([], 0.0, None),
        "charged": ([unknown], 1.5, None),
        "recorded-free-local": ([unknown], 0.0, "free_local"),
    }
    path = _store(
        tmp_path,
        tuple(_delegation(key, cost_tier_name="cheap_cloud") for key in examples),
    )
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE delegation_events ADD COLUMN attempt_history")
        db.execute("ALTER TABLE delegation_events ADD COLUMN cost_measurement_source")
        for key, (attempts, cost, source) in examples.items():
            db.execute(
                "UPDATE delegation_events SET attempt_history=?, cost_usd=?, "
                "cost_measurement_source=?, modelled_savings_usd='0.045068', "
                "savings_measurement='measured' WHERE correlation_id=?",
                (
                    attempts if isinstance(attempts, str) else json.dumps(attempts),
                    cost,
                    source,
                    key,
                ),
            )
    topic = "onex.snapshot.projection.delegation.decisions.v1"
    result = asyncio.run(
        HandlerProjectionRead(
            topic_map=_TOPICS, row_source=SqliteTableRowSource(path)
        ).handle(ModelProjectionReadRequest(topic=topic, tenant_id=_TENANT))
    )
    assert result.ok, result.response
    assert all("attempt_history" not in row for row in result.rows)
    rows = {
        r["correlation_id"]: (
            r["cost_usd"],
            r["modelled_savings_usd"],
            r["savings_measurement"],
        )
        for r in result.rows
    }
    assert rows.pop("unpriced") == (None, None, "unknown_spend")
    assert rows == {
        key: (cost, "0.045068", "measured")
        for key, (_, cost, _) in examples.items()
        if key != "unpriced"
    }
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT cost_usd, modelled_savings_usd, savings_measurement "
            "FROM delegation_events WHERE correlation_id='unpriced'"
        ).fetchone() == (0.0, "0.045068", "measured")
