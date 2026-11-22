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
import os
import sqlite3
import threading
import time
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
    # Free: a ":free" model id on a metered cloud tier (cheap_frontier would be
    # free by its tier alone and never reach the id).
    "free-id": (
        "cheap_cloud",
        "thinkingmachines/inkling:free",
        0.0,
        None,
        _UNKNOWN,
        False,
    ),
    # A tier or :free suffix written in another case or with spaces is still free.
    "local-tier-spelling": (" Local ", "qwen3.5:0.8b", 0.0, None, _UNKNOWN, False),
    "free-id-upper-case": (
        "cheap_cloud",
        "vendor/model:FREE",
        0.0,
        None,
        _UNKNOWN,
        False,
    ),
    # No tier recorded: nothing shows the run was cloud-priced, so it is served as stored.
    "no-tier-unpriced-model": (None, "Beam-1.0-501B-A23B", 0.0, None, _MEASURED, False),
    # Free: the manifest declares a zero price, on a metered tier.
    "declared-zero-price": ("cheap_cloud", "Qwen3.8-27B", 0.0, None, _UNKNOWN, False),
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


def test_a_store_without_the_cost_source_column_still_serves_an_unpriced_zero_as_unknown(
    tmp_path: Path,
) -> None:
    # Older stores predate cost_measurement_source. The read passes NULL for a
    # column the store lacks; were the missing name quoted instead, SQLite would
    # read it as a string literal, the source would look recorded and the
    # unpriced zero would be served as $0. A store without a column the
    # exposure declares (cost_tier_name) is refused by name, so it is not
    # exercised here.
    path = _store(
        tmp_path,
        (
            _delegation(
                "old-beam",
                cost_tier_name="cheap_cloud",
                model_name="Beam-1.0-501B-A23B",
                delegated_to="Beam-1.0-501B-A23B",
                cost_usd=0.0,
            ),
            _delegation(
                "old-local",
                cost_tier_name="local",
                model_name="qwen3.5:0.8b",
                delegated_to="qwen3.5:0.8b",
                cost_usd=0.0,
            ),
        ),
    )
    with sqlite3.connect(path) as db:
        columns = {r[1] for r in db.execute("PRAGMA table_info(delegation_events)")}
    assert "cost_measurement_source" not in columns
    served = {str(r["correlation_id"]): r for r in _read(path)}
    assert (
        served["old-beam"]["cost_usd"],
        served["old-beam"]["savings_measurement"],
    ) == (None, "unknown_spend")
    assert served["old-local"]["cost_usd"] == 0.0


def _attempt(
    tier: str, backend: str, model: str, *, accepted: bool, http_status: int | None
) -> dict[str, object]:
    return {
        "tier": tier,
        "backend_id": backend,
        "model_id": model,
        "quality_gate_passed": accepted,
        "cost_usd": 0.0,
        "usage_source": "measured" if http_status == 200 else None,
        "http_status": http_status,
        "acceptance_decision": "accept" if accepted else "climb",
    }


def test_a_billed_unpriced_rejected_attempt_makes_the_whole_run_unknown(
    tmp_path: Path,
) -> None:
    # Operator ruling 23413-G6 (2026-11-22): a Beam attempt answered HTTP 200
    # and was rejected, then a free attempt answered. The writer stored the run
    # at 0, with the free tier's record, because it counts the unpriced attempt
    # as 0; the Beam call was billed at a price no source states, so the whole
    # run's cost is unknown.
    answer = _attempt(
        "cheap_frontier",
        "gemini-flash-lite",
        "gemini-3.5-flash-lite",
        accepted=True,
        http_status=200,
    )
    beam = _attempt(
        "cheap_cloud",
        "byok-reflection",
        "Beam-1.0-501B-A23B",
        accepted=False,
        http_status=200,
    )
    free_run = {
        "cost_tier_name": "cheap_frontier",
        "cost_tier_type": "free_local",
        "cost_measurement_source": "free_local",
        "model_name": "gemini-3.5-flash-lite",
        "delegated_to": "gemini-3.5-flash-lite",
    }
    served = _served(
        tmp_path,
        {
            "billed-unpriced": {**free_run, "attempt_history": [beam, answer]},
            # Refused with HTTP 429: the provider never answered, nothing was billed.
            "refused": {
                **free_run,
                "attempt_history": [{**beam, "http_status": 429}, answer],
            },
            # A rejected attempt on a ":free" model was billed nothing.
            "billed-free": {
                **free_run,
                "attempt_history": [
                    _attempt(
                        "cheap_cloud",
                        "byok-openrouter",
                        "vendor/model:free",
                        accepted=False,
                        http_status=200,
                    ),
                    answer,
                ],
            },
            "no-history": free_run,
            "damaged-history": {**free_run, "attempt_history": {"not": "a ladder"}},
        },
    )
    assert served == {
        "billed-unpriced": _UNKNOWN_SPEND,
        "refused": _FREE,
        "billed-free": _FREE,
        "no-history": _FREE,
        "damaged-history": _FREE,
    }


@pytest.fixture
def models_dev_home(tmp_path: Path) -> Path:
    """The ONEX home a test's live models.dev read caches under (never the real one)."""
    return tmp_path / "onex_home"


def _models_dev(price: float) -> dict[str, object]:
    # The shape of https://models.dev/api.json, reduced to the one model.
    return {
        "reflection": {
            "models": {
                "Beam-1.0-501B-A23B": {"cost": {"input": price, "output": price}}
            }
        }
    }


def test_a_historical_beam_row_is_priced_from_a_live_models_dev_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, models_dev_home: Path
) -> None:
    # Operator ruling 23413-G7 (2026-11-22; AC34 and AC35 stay as written):
    # with no price from the provider, a run is priced from models.dev's price
    # for that provider and model, read live into a copy never older than the
    # configured age, never from the price list committed with the install
    # (omnimarket#6497), which lists Beam at [0, 0] and is in play here.
    from omnimarket.handlers import model_catalog

    settings = model_catalog.catalog_config()["models_dev"]
    max_age = float(settings["max_age_hours"])
    monkeypatch.setattr(
        model_catalog, "pricing_models_dev_index", model_catalog.shipped_pricing_index
    )
    beam = {"cost_tier_name": "cheap_cloud", "model_name": "Beam-1.0-501B-A23B"}
    rows = {
        "by-delegated-to": {**beam, "delegated_to": "byok-reflection"},
        "by-backend-id": {
            **beam,
            "delegated_to": "Beam-1.0-501B-A23B",
            "backend_id": "byok-reflection",
        },
    }

    def scenario(
        name: str,
        document: dict[str, object] | None,
        cached: dict[str, object] | None = None,
        cache_age_hours: float = 0.0,
    ) -> tuple[dict[str, tuple[object, object, object]], int]:
        home = models_dev_home / name
        monkeypatch.setattr(model_catalog, "_onex_home", lambda: home)
        fetched: list[str] = []

        def fetch(url: str, timeout: float) -> bytes:
            fetched.append(url)
            if document is None:
                raise OSError("models.dev unreachable")
            return json.dumps(document).encode("utf-8")

        monkeypatch.setattr(model_catalog, "_fetch", fetch)
        if cached is not None:
            cache = home / str(settings["cache_file"])
            cache.parent.mkdir(parents=True)
            cache.write_text(json.dumps(cached), encoding="utf-8")
            stamp = time.time() - cache_age_hours * 3600
            os.utime(cache, (stamp, stamp))
        store = tmp_path / name
        store.mkdir()
        return _served(store, rows), len(fetched)

    free = {"by-delegated-to": _FREE, "by-backend-id": _FREE}
    unknown = {"by-delegated-to": _UNKNOWN_SPEND, "by-backend-id": _UNKNOWN_SPEND}
    # A served read runs two queries (the window, then its freshness), and
    # models.dev is read at most once per query, never once per row: the first
    # query's fetch leaves a fresh copy the second reuses, and a failed fetch
    # is not tried again within the retry interval.
    assert {
        "live": scenario("live", _models_dev(0)),
        # A copy younger than the configured age is reused without a fetch.
        "kept": scenario("kept", None, _models_dev(0), max_age - 1),
        # An older copy whose refetch fails is not reused.
        "expired": scenario("expired", None, _models_dev(0), max_age + 1),
        # A positive price does not make a stored zero free.
        "priced": scenario("priced", _models_dev(3)),
        # Only the shipped price list: unknown.
        "shipped-only": scenario("shipped-only", None),
    } == {
        "live": (free, 1),
        "kept": (free, 0),
        "expired": (unknown, 1),
        "priced": (unknown, 1),
        "shipped-only": (unknown, 1),
    }


_BEAM_ROWS = {
    "beam": {
        "cost_tier_name": "cheap_cloud",
        "model_name": "Beam-1.0-501B-A23B",
        "delegated_to": "byok-reflection",
    }
}


def test_a_hanging_models_dev_costs_a_read_its_deadline_once_per_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, models_dev_home: Path
) -> None:
    # Review of bc576d96: a failed models.dev read recorded nothing, and a
    # served read runs two queries, so with models.dev hanging and no fresh copy
    # every dashboard refresh and every onex metering run stalled about 20 s.
    # A hanging read now costs at most a deadline well under the 10 s fetch
    # timeout, once per retry interval, and the run stays unknown.
    from omnimarket.handlers import model_catalog

    hang_seconds = 4.0
    release = threading.Event()
    fetched: list[str] = []

    def hanging_fetch(url: str, timeout: float) -> bytes:
        fetched.append(url)
        release.wait(hang_seconds)
        raise TimeoutError("models.dev did not answer")

    monkeypatch.setattr(model_catalog, "_onex_home", lambda: models_dev_home)
    monkeypatch.setattr(model_catalog, "_fetch", hanging_fetch)

    def timed_read(name: str) -> tuple[object, int, float]:
        store = tmp_path / name
        store.mkdir()
        started = time.monotonic()
        served = _served(store, _BEAM_ROWS)["beam"]
        return served, len(fetched), time.monotonic() - started

    try:
        first, after_first, first_seconds = timed_read("first")
        second, after_second, second_seconds = timed_read("second")
        # Once the retry interval has passed, models.dev is tried again, once.
        day_ago = time.time() - 86400
        for kept in models_dev_home.rglob("*"):
            if kept.is_file():
                os.utime(kept, (day_ago, day_ago))
        third, after_third, _ = timed_read("third")
    finally:
        release.set()

    assert (first, second, third) == (_UNKNOWN_SPEND,) * 3
    assert (after_first, after_second, after_third) == (1, 1, 2)
    assert first_seconds < hang_seconds
    assert second_seconds < 1.0


def test_a_fresh_models_dev_copy_that_cannot_be_parsed_is_read_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, models_dev_home: Path
) -> None:
    # Review of bc576d96: a cached copy younger than its age but not JSON gave
    # an empty index, so every Beam run read unknown until the copy aged out.
    from omnimarket.handlers import model_catalog

    settings = model_catalog.catalog_config()["models_dev"]
    cache = models_dev_home / str(settings["cache_file"])
    cache.parent.mkdir(parents=True)
    cache.write_text("{not json", encoding="utf-8")
    fetched: list[str] = []

    def fetch(url: str, timeout: float) -> bytes:
        fetched.append(url)
        return json.dumps(_models_dev(0)).encode("utf-8")

    monkeypatch.setattr(model_catalog, "_onex_home", lambda: models_dev_home)
    monkeypatch.setattr(model_catalog, "_fetch", fetch)

    assert (_served(tmp_path, _BEAM_ROWS)["beam"], len(fetched)) == (_FREE, 1)
    assert json.loads(cache.read_text(encoding="utf-8")) == _models_dev(0)
