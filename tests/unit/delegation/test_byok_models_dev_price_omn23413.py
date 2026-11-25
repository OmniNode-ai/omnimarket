# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A customer-key call is priced from the models.dev copy kept on the machine, else the
price list shipped in the install, when neither the provider catalogue nor the model
registry prices its model. It never reads models.dev itself.

Reflection's own `/openai/v1/models` and chat responses carry no price, while
models.dev lists reflection/Beam-1.0-501B-A23B at input 0, output 0. These tests pin
the kept-copy price for a model the catalogue leaves unpriced, a paid price, a model
no source lists, and the shipped-list fallback.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnimarket.enums.enum_cost_basis import EnumCostBasis
from omnimarket.enums.enum_cost_source import EnumCostSource
from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.events.llm_delegation_call import ModelLlmDelegationCallRequest
from omnimarket.handlers import model_catalog
from omnimarket.handlers.model_catalog import ModelsDevIndex
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call as call_handler,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
    _compute_cost,
    _cost_usage_source,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_result import (
    ModelLlmDelegationCallResult,
)

pytestmark = pytest.mark.unit

REFLECTION = "https://api.reflection.ai/openai/v1/chat/completions"
BEAM = "Beam-1.0-501B-A23B"


def _index(models: dict[tuple[str, str], dict[str, object]]) -> ModelsDevIndex:
    return ModelsDevIndex(
        models=models, fetched_at="2026-11-14T03:36:52+00:00", stale=False, error=None
    )


ServeModelsDev = Callable[[dict[tuple[str, str], dict[str, object]]], None]


@pytest.fixture
def models_dev(monkeypatch: pytest.MonkeyPatch) -> ServeModelsDev:
    def serve(models: dict[tuple[str, str], dict[str, object]]) -> None:
        monkeypatch.setattr(
            model_catalog, "pricing_models_dev_index", lambda: _index(models)
        )

    return serve


def test_models_dev_zero_price_books_a_beam_call_as_free_and_measured(
    models_dev: ServeModelsDev,
) -> None:
    models_dev(
        {
            ("reflection", BEAM): {
                "id": BEAM,
                "cost": {"input": 0, "output": 0, "cache_read": 0},
            }
        }
    )
    actual, _opus, savings, cost_basis = _compute_cost(
        BEAM,
        1482,
        6056,
        model_tier="cheap_cloud",
        endpoint_url=REFLECTION,
        provider="byok-reflection",
    )
    assert actual == Decimal("0")
    assert savings > 0
    assert cost_basis is EnumCostBasis.ZERO_MARGINAL_API_COST
    assert _cost_usage_source(cost_basis) is EnumUsageSource.MEASURED


def test_a_paid_models_dev_price_is_booked_as_cloud_cost(
    models_dev: ServeModelsDev,
) -> None:
    models_dev(
        {("reflection", BEAM): {"id": BEAM, "cost": {"input": 0.3, "output": 1.0}}}
    )
    actual, _opus, _savings, cost_basis = _compute_cost(
        BEAM,
        1_000_000,
        1_000_000,
        model_tier="cheap_cloud",
        endpoint_url=REFLECTION,
        provider="byok-reflection",
    )
    assert actual == Decimal("1.3")
    assert cost_basis is EnumCostBasis.CLOUD_API_COST


def test_a_model_models_dev_does_not_list_stays_unpriced(
    models_dev: ServeModelsDev,
) -> None:
    models_dev({})
    actual, _opus, savings, cost_basis = _compute_cost(
        BEAM,
        1482,
        6056,
        model_tier="cheap_cloud",
        endpoint_url=REFLECTION,
        provider="byok-reflection",
    )
    assert actual == Decimal("0")
    assert savings == Decimal("0")
    assert cost_basis is EnumCostBasis.UNKNOWN
    assert _cost_usage_source(cost_basis) is EnumUsageSource.UNKNOWN


def test_a_models_dev_entry_without_a_price_stays_unpriced(
    models_dev: ServeModelsDev,
) -> None:
    models_dev({("reflection", BEAM): {"id": BEAM}})
    _actual, _opus, _savings, cost_basis = _compute_cost(
        BEAM,
        1482,
        6056,
        model_tier="cheap_cloud",
        endpoint_url=REFLECTION,
        provider="byok-reflection",
    )
    assert cost_basis is EnumCostBasis.UNKNOWN


# --- A call prices from what is kept on the machine and never reads models.dev
# (operator ruling 2026-11-23: the price list is read at setup and by the refresh
# command, never during a customer session).
#
# Failure modes these tests catch:
#   N1  a call reads models.dev, so a customer session connects to a host other
#       than the provider it chose.
#   N2  a kept copy older than the configured age is refused, so a session that
#       cannot renew it loses its prices.
#   N3  with no kept copy the call is unpriced instead of using the shipped list.
#   N4  the call result does not say which source priced it or when it was read.
#   N5  a provider's own price loses to models.dev, or is recorded under the wrong source.

_PRODUCTION_PRICING_INDEX = model_catalog.pricing_models_dev_index


class _ModelsDevServer:
    """A recorded models.dev transport: serves one document and counts reads."""

    def __init__(self) -> None:
        self.document: dict[str, object] | None = None
        self.reads: list[str] = []

    def __call__(self, url: str, timeout: float) -> bytes:
        self.reads.append(url)
        if self.document is None:
            raise OSError("models.dev unreachable in this test")
        return json.dumps(self.document).encode()


def _models_dev_document(model: str, price: tuple[float, float]) -> dict[str, object]:
    return {
        "reflection": {
            "models": {
                model: {"id": model, "cost": {"input": price[0], "output": price[1]}}
            }
        }
    }


@pytest.fixture
def live_models_dev(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> _ModelsDevServer:
    """The production pricing read over a recorded transport and an isolated ONEX home."""
    server = _ModelsDevServer()
    monkeypatch.setattr(model_catalog, "_onex_home", lambda: tmp_path)
    monkeypatch.setattr(model_catalog, "_fetch", server)
    monkeypatch.setattr(
        model_catalog, "pricing_models_dev_index", _PRODUCTION_PRICING_INDEX
    )
    return server


def _models_dev_cache(tmp_path: Path) -> Path:
    return tmp_path / str(model_catalog.catalog_config()["models_dev"]["cache_file"])


def _keep_copy(tmp_path: Path, document: dict[str, object], *, age: bool) -> None:
    cache = _models_dev_cache(tmp_path)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(document), encoding="utf-8")
    if age:
        os.utime(cache, (0, 0))  # 1970: older than any configured age


def _call(
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider: str,
    model: str,
    timeout_seconds: float = 60.0,
    endpoint: str = "http://localhost:8000",
    posts: list[dict[str, object]] | None = None,
) -> ModelLlmDelegationCallResult:
    def post(**kwargs: object) -> transport.ModelTransportResponse:
        if posts is not None:
            posts.append(dict(kwargs))
        return transport.ModelTransportResponse(
            status_code=200,
            json_body={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
            },
            latency_ms=1,
        )

    monkeypatch.setattr(transport, "post_chat_completion", post)
    monkeypatch.setattr(call_handler, "_is_endpoint_healthy", lambda *_a, **_k: True)
    monkeypatch.setattr(call_handler, "_get_served_model_ids", lambda *_a, **_k: None)
    return call_handler.HandlerLlmDelegationCall()(
        ModelLlmDelegationCallRequest(
            request_id="req-ac35",
            correlation_id="corr-ac35",
            causation_id="caus-ac35",
            model_id=model,
            endpoint_ref=endpoint,
            prompt="Explain a hash map.",
            prompt_hash="ac35",
            task_type="document",
            model_tier="cheap_cloud",
            provider=provider,
            timeout_seconds=timeout_seconds,
        )
    )


def test_a_beam_call_prices_from_the_kept_copy_with_no_models_dev_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, live_models_dev: _ModelsDevServer
) -> None:
    """Beam on a Reflection key, booked at 0 from the kept copy, source models.dev."""
    _keep_copy(tmp_path, _models_dev_document(BEAM, (0, 0)), age=False)
    before = datetime.now(UTC)
    result = _call(monkeypatch, provider="byok-reflection", model=BEAM)
    after = datetime.now(UTC)
    assert live_models_dev.reads == [], "the call read models.dev"
    assert result.success
    assert result.actual_cost_usd == Decimal("0")
    assert result.cost_basis is EnumCostBasis.ZERO_MARGINAL_API_COST
    assert result.cost_source is EnumCostSource.ESTIMATED
    assert result.price_source == "models.dev"
    assert result.price_read_at is not None
    assert (
        before - timedelta(seconds=5)
        <= datetime.fromisoformat(result.price_read_at)
        <= after
    )


def test_a_kept_copy_older_than_the_configured_age_still_prices_the_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, live_models_dev: _ModelsDevServer
) -> None:
    """A session cannot renew the copy, so its age never refuses it (setup and refresh do)."""
    _keep_copy(tmp_path, _models_dev_document(BEAM, (0.3, 1.0)), age=True)
    live_models_dev.document = _models_dev_document(BEAM, (5, 5))
    result = _call(monkeypatch, provider="byok-reflection", model=BEAM)
    assert live_models_dev.reads == [], "the call read models.dev"
    assert result.actual_cost_usd == Decimal("1.3")
    assert result.cost_basis is EnumCostBasis.CLOUD_API_COST
    assert result.price_source == "models.dev"
    assert result.price_read_at == datetime.fromtimestamp(0, UTC).isoformat()


def test_with_no_kept_copy_the_call_prices_from_the_shipped_list_and_says_estimated(
    monkeypatch: pytest.MonkeyPatch, live_models_dev: _ModelsDevServer
) -> None:
    """No copy under the ONEX home: the shipped list prices it, marked estimated."""
    shipped = model_catalog.shipped_pricing_index()
    price = shipped.models[("openrouter", "qwen/qwen-plus")]["cost"]
    expected = (Decimal(str(price["input"])) + Decimal(str(price["output"]))) * 1
    result = _call(monkeypatch, provider="byok-openrouter", model="qwen/qwen-plus")
    assert live_models_dev.reads == [], "the call read models.dev"
    assert result.actual_cost_usd == expected
    assert result.cost_basis is EnumCostBasis.CLOUD_API_COST
    assert result.cost_source is EnumCostSource.ESTIMATED
    assert result.price_source == "shipped price list"
    assert result.price_read_at == shipped.fetched_at


def test_a_model_absent_from_every_source_is_left_unknown_with_no_read(
    monkeypatch: pytest.MonkeyPatch, live_models_dev: _ModelsDevServer
) -> None:
    result = _call(monkeypatch, provider="byok-reflection", model="absent-model-zzz")
    assert live_models_dev.reads == []
    assert result.cost_basis is EnumCostBasis.UNKNOWN
    assert result.price_source is None
    assert result.price_read_at is None


def test_the_providers_own_price_wins_and_is_recorded_with_its_read_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, live_models_dev: _ModelsDevServer
) -> None:
    """AC35: the provider's own price when it serves one, recorded as that source."""
    from omnimarket.handlers.routing.byok_model_listing import list_byok_models

    model = "vendor/fixture-model"
    live_models_dev.document = {
        "openrouter": {
            "models": {model: {"id": model, "cost": {"input": 9, "output": 9}}}
        }
    }
    listing = list_byok_models(
        "openrouter",
        "fixture",
        get=lambda **_: {
            "data": [
                {
                    "id": model,
                    "pricing": {"prompt": "0.000002", "completion": "0.000003"},
                }
            ]
        },
    )
    model_catalog.remember_provider_listing(listing)
    kept = tmp_path / str(
        model_catalog.catalog_config()["provider_listing"]["cache_file"]
    ).format(provider="openrouter")
    kept_at = datetime.fromtimestamp(kept.stat().st_mtime, UTC)
    result = _call(monkeypatch, provider="byok-openrouter", model=model)
    assert result.actual_cost_usd == Decimal("5")
    assert result.cost_basis is EnumCostBasis.CLOUD_API_COST
    assert result.price_source == "openrouter"
    assert result.price_read_at == kept_at.isoformat()


def test_a_models_dev_price_without_a_combined_entry_records_the_read_time_of_that_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[str] = []

    def _counting() -> ModelsDevIndex:
        reads.append("models.dev")
        return _index(
            {
                ("reflection", BEAM): {
                    "id": BEAM,
                    "cost": {"input": 0.3, "output": 1.0},
                }
            }
        )

    monkeypatch.setattr(model_catalog, "pricing_models_dev_index", _counting)
    monkeypatch.setattr(
        call_handler,
        "catalog_config",
        lambda: {**model_catalog.catalog_config(), "provider_ids": {}},
    )
    result = _call(monkeypatch, provider="byok-reflection", model=BEAM)
    assert result.actual_cost_usd == Decimal("1.3")
    assert result.price_source == "models.dev"
    assert result.price_read_at == "2026-11-14T03:36:52+00:00"
    assert len(reads) == 1


@pytest.mark.parametrize(
    ("provider", "model", "endpoint", "source", "cost"),
    [
        (
            "byok-glm-general",
            "glm-5.3-flash",
            "https://api.z.ai/api/paas/v4/chat/completions",
            "byok_provider_backends",
            None,
        ),
        (
            "byok-openrouter",
            "qwen/qwen3.8-27b",
            "http://localhost:8000",
            "model_registry",
            Decimal("2.025"),
        ),
    ],
)
def test_each_packaged_price_source_is_recorded_without_a_read_time(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    model: str,
    endpoint: str,
    source: str,
    cost: Decimal | None,
) -> None:
    result = _call(
        monkeypatch,
        provider=provider,
        model=model,
        endpoint=endpoint,
    )
    assert result.price_source == source
    assert result.price_read_at is None
    assert result.cost_basis is not EnumCostBasis.UNKNOWN
    if cost is not None:
        assert result.actual_cost_usd == cost


def test_an_eval_arm_price_is_recorded_as_the_routing_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        call_handler,
        "load_delegation_routing_overlay",
        lambda: SimpleNamespace(
            eval_arm_price_per_1m=lambda _e, _m: (Decimal("1"), Decimal("2")),
            declared_price_per_1m=lambda _e, _m: None,
        ),
    )
    result = _call(monkeypatch, provider="byok-reflection", model="arm-model-zzz")
    assert result.actual_cost_usd == Decimal("3")
    assert result.price_source == "delegation_routing_overlay"
    assert result.price_read_at is None
