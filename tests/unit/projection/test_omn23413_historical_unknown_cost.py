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
    # The routing registry declares cheap_frontier free_local, so its zero is served as stored.
    "free-tier-unpriced-model": (
        "cheap_frontier",
        "gemini-3.5-flash-lite",
        0.0,
        None,
        _MEASURED,
        False,
    ),
    # A tier the routing registry does not declare has no cost model, which is
    # not a free tier: an unpriced model's zero on it is unknown.
    "unregistered-tier": (
        "no_such_tier",
        "Beam-1.0-501B-A23B",
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


_DECISIONS = "onex.snapshot.projection.delegation.decisions.v1"
_TRACE = "onex.snapshot.projection.delegation.correlation-trace.v1"


def _read(
    path: Path, topic: str = _DECISIONS, correlation_id: str | None = None
) -> list[dict[str, object]]:
    result = asyncio.run(
        HandlerProjectionRead(
            topic_map=_TOPICS, row_source=SqliteTableRowSource(path)
        ).handle(
            ModelProjectionReadRequest(
                topic=topic, tenant_id=_TENANT, row_correlation_id=correlation_id
            )
        )
    )
    assert result.ok, result.response
    return list(result.rows)


def _served(
    tmp_path: Path, rows: dict[str, dict[str, object]]
) -> dict[str, tuple[object, object, object]]:
    """Each stored zero-cost run's served (cost, saving, measurement), keyed by correlation id."""
    stored = {
        "cost_usd": 0.0,
        "modelled_savings_usd": _SAVED,
        "savings_measurement": "measured",
    }
    path = _store(
        tmp_path,
        tuple(_delegation(key, **{**stored, **fields}) for key, fields in rows.items()),
    )
    return {
        str(r["correlation_id"]): (
            r["cost_usd"],
            r["modelled_savings_usd"],
            r["savings_measurement"],
        )
        for r in _read(path)
    }


_FREE = (0.0, _SAVED, "measured")
_UNKNOWN_SPEND = (None, None, "unknown_spend")


def test_a_positive_manifest_price_does_not_make_a_stored_zero_free(
    tmp_path: Path,
) -> None:
    # The manifest prices gpt-4o above zero, so 213 tokens in and 6 out cost
    # more than nothing: a stored zero that no cost model priced is unknown.
    # Only a declared zero price (Qwen3.8-27B) shows the run was free.
    served = _served(
        tmp_path,
        {
            "priced": {
                "cost_tier_name": "cheap_cloud",
                "model_name": "gpt-4o",
                "delegated_to": "gpt-4o",
                "cost_measurement_source": "no_cost_model",
            },
            "declared-zero": {
                "cost_tier_name": "cheap_cloud",
                "model_name": "Qwen3.8-27B",
                "delegated_to": "Qwen3.8-27B",
            },
        },
    )
    assert served == {"priced": _UNKNOWN_SPEND, "declared-zero": _FREE}


def test_the_row_s_own_cost_record_wins_over_today_s_tier_registry(
    tmp_path: Path,
) -> None:
    # A tier with no cost model (the tenant overlay always resolves to none) is
    # not a free tier, and a row that records no cost model priced it is not
    # made free by its tier; a row recording a free_local tier type is free
    # whatever today's registry says of its tier name.
    beam = {"model_name": "Beam-1.0-501B-A23B", "delegated_to": "byok-reflection"}
    served = _served(
        tmp_path,
        {
            "overlay": {
                **beam,
                "cost_tier_name": "tenant_overlay",
                "cost_measurement_source": "no_cost_model",
            },
            "no-tier": {
                **beam,
                "cost_tier_name": None,
                "cost_measurement_source": "no_cost_model",
            },
            "recorded-free": {
                **beam,
                "cost_tier_name": "cheap_cloud",
                "cost_tier_type": "free_local",
            },
        },
    )
    assert served == {
        "overlay": _UNKNOWN_SPEND,
        "no-tier": _UNKNOWN_SPEND,
        "recorded-free": _FREE,
    }


def test_a_backend_alias_does_not_hide_the_free_model_that_answered(
    tmp_path: Path,
) -> None:
    # delegated_to names the backend, routed_model the ":free" model that
    # answered; on a metered tier the free id alone makes the zero free.
    served = _served(
        tmp_path,
        {
            "alias": {
                "cost_tier_name": "cheap_cloud",
                "model_name": None,
                "delegated_to": "byok-openrouter",
                "routed_model": "vendor/model:free",
            },
            "alias-unpriced": {
                "cost_tier_name": "cheap_cloud",
                "model_name": None,
                "delegated_to": "byok-openrouter",
                "routed_model": "vendor/model",
            },
        },
    )
    assert served == {"alias": _FREE, "alias-unpriced": _UNKNOWN_SPEND}


def test_a_zero_price_the_provider_catalogue_declares_is_free(tmp_path: Path) -> None:
    # byok_provider_backends.v1.yaml declares glm-4.5-flash free on the z.ai
    # general API (backend byok-glm-general) and glm-5.3-flash priced; the
    # pricing manifest lists neither. The backend may be named by delegated_to
    # or by backend_id.
    served = _served(
        tmp_path,
        {
            "glm-free": {
                "cost_tier_name": "cheap_cloud",
                "model_name": "glm-4.5-flash",
                "delegated_to": "byok-glm-general",
            },
            "glm-free-by-backend-id": {
                "cost_tier_name": "cheap_cloud",
                "model_name": "glm-4.5-flash",
                "delegated_to": "glm-4.5-flash",
                "backend_id": "byok-glm-general",
            },
            "glm-priced": {
                "cost_tier_name": "cheap_cloud",
                "model_name": "glm-5.3-flash",
                "delegated_to": "byok-glm-general",
            },
        },
    )
    assert served == {
        "glm-free": _FREE,
        "glm-free-by-backend-id": _FREE,
        "glm-priced": _UNKNOWN_SPEND,
    }


def test_the_run_detail_serves_no_saving_for_an_unknown_cost(tmp_path: Path) -> None:
    # The run-detail exposure serves the legacy cost_savings_usd, not
    # modelled_savings_usd; an unknown cost must not carry a saving there either.
    path = _store(
        tmp_path,
        (
            _delegation(
                "beam",
                cost_tier_name="cheap_cloud",
                model_name="Beam-1.0-501B-A23B",
                delegated_to="Beam-1.0-501B-A23B",
                cost_usd=0.0,
                cost_savings_usd=float(_SAVED),
            ),
        ),
    )
    [row] = _read(path, _TRACE, "beam")
    assert (row["cost_usd"], row["cost_savings_usd"]) == (None, None)


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
