# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23413 Amendment 10, AC35: a customer-key call is priced from models.dev when
neither the provider catalogue nor the model registry prices its model.

Read live on 2026-11-14: Reflection's own `/openai/v1/models` and chat responses
carry no price, while models.dev lists reflection/Beam-1.0-501B-A23B at input 0,
output 0. Before this change every Beam call on a Reflection key was booked with
cost basis UNKNOWN and usage source unknown, which the Runs page shows as
"unknown". These tests pin the models.dev price for a model the catalogue leaves
unpriced, a paid models.dev price, and a model models.dev does not list.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnimarket.enums.enum_cost_basis import EnumCostBasis
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


# --- AC35 as written (operator ruling G1-NO, 2026-11-22): a call reads models.dev
# live, a copy older than the configured age is read again and never prices a
# call, and each call records which source priced it and when that source was read.
#
# Failure modes these tests catch:
#   L1  the call prices from a committed copy (the shipped list) and never reads
#       models.dev, so a price models.dev changes is not followed.
#   L2  a copy older than the configured age prices the call instead of being read again.
#   L3  a stale copy whose refresh fails still prices the call.
#   L4  the call result does not say which source priced it or when it was read.
#   L5  a provider's own price loses to models.dev, or is recorded under the wrong source.

_PRODUCTION_PRICING_INDEX = model_catalog.pricing_models_dev_index


class _ModelsDevServer:
    """A recorded models.dev transport: serves one document, or fails, and counts reads."""

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


def _keep_expired_copy(tmp_path: Path, document: dict[str, object]) -> None:
    cache = _models_dev_cache(tmp_path)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(document), encoding="utf-8")
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


def test_the_models_dev_read_lock_is_created_readable_by_its_owner_alone(
    live_models_dev: _ModelsDevServer, tmp_path: Path
) -> None:
    """CodeQL py/overly-permissive-file: the lock must not be world readable.

    Run under umask 0 so the mode the code asks for is the mode the file gets;
    a 0o644 request would otherwise be masked down by the shell's umask.
    """
    live_models_dev.document = _models_dev_document(BEAM, (0, 0))
    previous = os.umask(0)
    try:
        model_catalog.models_dev_price_with_read_time("reflection", BEAM)
    finally:
        os.umask(previous)
    lock = model_catalog._read_lock_path(tmp_path)
    assert lock.is_file(), "the read did not create its lock under the ONEX home"
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_a_beam_call_reads_models_dev_live_and_records_it_as_the_price_source(
    monkeypatch: pytest.MonkeyPatch, live_models_dev: _ModelsDevServer
) -> None:
    """AC35 falsifier: Beam on a Reflection key, booked at 0, zero_marginal_api_cost, source models.dev."""
    live_models_dev.document = _models_dev_document(BEAM, (0, 0))
    before = datetime.now(UTC)
    result = _call(monkeypatch, provider="byok-reflection", model=BEAM)
    after = datetime.now(UTC)
    assert len(live_models_dev.reads) == 1, "the call did not read models.dev live"
    assert result.success
    assert result.actual_cost_usd == Decimal("0")
    assert result.cost_basis is EnumCostBasis.ZERO_MARGINAL_API_COST
    assert result.price_source == "models.dev"
    assert result.price_read_at is not None
    assert before <= datetime.fromisoformat(result.price_read_at) <= after


def test_a_models_dev_copy_older_than_the_configured_age_is_read_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, live_models_dev: _ModelsDevServer
) -> None:
    """AC35 falsifier: a source older than the configured age is read again."""
    _keep_expired_copy(tmp_path, _models_dev_document(BEAM, (5, 5)))
    live_models_dev.document = _models_dev_document(BEAM, (0.3, 1.0))
    first = _call(monkeypatch, provider="byok-reflection", model=BEAM)
    assert len(live_models_dev.reads) == 1, "the expired copy was not read again"
    assert first.actual_cost_usd == Decimal("1.3")
    assert first.cost_basis is EnumCostBasis.CLOUD_API_COST
    # Within the configured age the copy just read is reused, not read again.
    second = _call(monkeypatch, provider="byok-reflection", model=BEAM)
    assert len(live_models_dev.reads) == 1
    assert second.actual_cost_usd == Decimal("1.3")
    kept_at = datetime.fromtimestamp(_models_dev_cache(tmp_path).stat().st_mtime, UTC)
    assert second.price_read_at == kept_at.isoformat()


def test_an_expired_copy_that_cannot_be_read_again_never_prices_a_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, live_models_dev: _ModelsDevServer
) -> None:
    """AC35: a reused copy is never older than the configured age; unknown as AC28 says."""
    _keep_expired_copy(tmp_path, _models_dev_document(BEAM, (0.3, 1.0)))
    result = _call(monkeypatch, provider="byok-reflection", model=BEAM)
    assert len(live_models_dev.reads) == 1
    assert result.success
    assert result.cost_basis is EnumCostBasis.UNKNOWN
    assert result.actual_cost_usd == Decimal("0")
    assert result.price_source is None
    assert result.price_read_at is None


def test_a_model_absent_from_both_sources_is_left_unknown(
    monkeypatch: pytest.MonkeyPatch, live_models_dev: _ModelsDevServer
) -> None:
    """AC35 falsifier: a model absent from both sources left unknown, after a live read."""
    live_models_dev.document = _models_dev_document(BEAM, (0, 0))
    result = _call(monkeypatch, provider="byok-reflection", model="absent-model-zzz")
    assert len(live_models_dev.reads) == 1
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


def test_a_hanging_models_dev_read_is_cut_and_the_call_finishes_within_its_limit(
    monkeypatch: pytest.MonkeyPatch, live_models_dev: _ModelsDevServer
) -> None:
    reads: list[str] = []
    gate = threading.Event()

    def _blocking_fetch(url: str, timeout: float) -> bytes:
        reads.append(url)
        gate.wait(30.0)
        raise OSError("models.dev unreachable in this test")

    monkeypatch.setattr(model_catalog, "_fetch", _blocking_fetch)
    posts: list[dict[str, object]] = []
    started = time.monotonic()
    try:
        result = _call(
            monkeypatch,
            provider="byok-reflection",
            model=BEAM,
            timeout_seconds=5.0,
            posts=posts,
        )
    finally:
        gate.set()
    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert result.success
    assert result.cost_basis is EnumCostBasis.UNKNOWN
    assert result.price_source is None
    post_timeout = posts[0]["timeout_seconds"]
    assert isinstance(post_timeout, (int, float))
    assert post_timeout < 5.0
    assert len(reads) == 1
    _call(
        monkeypatch,
        provider="byok-reflection",
        model=BEAM,
        timeout_seconds=5.0,
        posts=posts,
    )
    assert len(reads) == 1


def test_a_failed_models_dev_read_is_not_retried_within_the_configured_interval(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    live_models_dev: _ModelsDevServer,
) -> None:
    live_models_dev.document = None
    _call(monkeypatch, provider="byok-reflection", model=BEAM)
    _call(monkeypatch, provider="byok-reflection", model=BEAM)
    assert len(live_models_dev.reads) == 1
    path = (
        tmp_path / str(model_catalog.catalog_config()["models_dev"]["cache_file"])
    ).with_suffix(".failed")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({}), encoding="utf-8")
    os.utime(path, (0, 0))
    _call(monkeypatch, provider="byok-reflection", model=BEAM)
    assert len(live_models_dev.reads) == 2


def test_the_model_budget_worker_cuts_a_hanging_read_and_shortens_the_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    live_models_dev: _ModelsDevServer,
) -> None:
    from omnimarket.handlers.routing import byok_provider_backends
    from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
        port_local_delegation_dispatch,
    )

    reads: list[str] = []
    gate = threading.Event()

    def _blocking_fetch(url: str, timeout: float) -> bytes:
        reads.append(url)
        gate.wait(30.0)
        raise OSError("models.dev unreachable in this test")

    monkeypatch.setattr(model_catalog, "_fetch", _blocking_fetch)

    seen: dict[str, object] = {}

    def _handler(
        request: ModelLlmDelegationCallRequest,
    ) -> ModelLlmDelegationCallResult:
        seen["request"] = request
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
        )

    row = byok_provider_backends.resolve_byok_provider_backend("reflection")
    assert row is not None
    started = time.monotonic()
    try:
        port_local_delegation_dispatch._call_with_model_budget(
            _handler,
            row,
            4096,
            ModelLlmDelegationCallRequest(
                request_id="req-ac35",
                correlation_id="corr-ac35",
                causation_id="caus-ac35",
                model_id=BEAM,
                endpoint_ref="http://localhost:8000",
                prompt="Explain a hash map.",
                prompt_hash="ac35",
                task_type="document",
                model_tier="cheap_cloud",
                provider="byok-reflection",
                timeout_seconds=5.0,
            ),
        )
    finally:
        gate.set()
    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert len(reads) == 1
    handled = seen["request"]
    assert isinstance(handled, ModelLlmDelegationCallRequest)
    assert handled.timeout_seconds < 5.0


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


# --- Round-2 review of 82de9581: one models.dev read in flight per ONEX home, and
# no failure marked when no read actually ran.
#
# Failure modes:
#   S1  concurrent calls that find the copy expired each start their own 6.7 MB read.
#   S2  a zero or tiny deadline marks a failure for the retry interval though no read ran.
#   S3  a caller that only waited on another call's read marks a failure when it gives up.

_PRICED = (Decimal("0.3"), Decimal("1.0"))


def _failure_marker_path(tmp_path: Path) -> Path:
    return _models_dev_cache(tmp_path).with_suffix(".failed")


def _join_price_readers() -> None:
    for thread in threading.enumerate():
        if thread.name == "models-dev-price-read":
            thread.join(5)


def test_concurrent_calls_share_one_models_dev_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, live_models_dev: _ModelsDevServer
) -> None:
    live_models_dev.document = _models_dev_document(BEAM, (0.3, 1.0))

    def slow(url: str, timeout: float) -> bytes:
        time.sleep(0.3)
        return live_models_dev(url, timeout)

    monkeypatch.setattr(model_catalog, "_fetch", slow)
    barrier = threading.Barrier(5)
    results: list[tuple[tuple[Decimal, Decimal], str | None] | None] = [None] * 5

    def worker(index: int) -> None:
        barrier.wait()
        results[index] = model_catalog.models_dev_price_with_read_time(
            "reflection", BEAM, deadline_seconds=3.0
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(live_models_dev.reads) == 1, "concurrent calls each read models.dev"
    assert [r[0] if r else None for r in results] == [_PRICED] * 5


def test_a_zero_deadline_attempts_no_read_and_marks_no_failure(
    tmp_path: Path, live_models_dev: _ModelsDevServer
) -> None:
    live_models_dev.document = _models_dev_document(BEAM, (0.3, 1.0))
    assert (
        model_catalog.models_dev_price_with_read_time(
            "reflection", BEAM, deadline_seconds=0.0
        )
        is None
    )
    _join_price_readers()
    assert live_models_dev.reads == [], "a zero deadline started a read"
    assert not _failure_marker_path(tmp_path).exists(), (
        "no read ran, yet a failure was marked"
    )
    found = model_catalog.models_dev_price_with_read_time(
        "reflection", BEAM, deadline_seconds=3.0
    )
    assert found is not None
    assert found[0] == _PRICED
    assert len(live_models_dev.reads) == 1


def test_a_caller_waiting_on_another_read_gives_up_without_marking_a_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, live_models_dev: _ModelsDevServer
) -> None:
    started = threading.Event()
    release = threading.Event()

    def held(url: str, timeout: float) -> bytes:
        live_models_dev.reads.append(url)
        started.set()
        release.wait(10)
        return json.dumps(_models_dev_document(BEAM, (0.3, 1.0))).encode()

    monkeypatch.setattr(model_catalog, "_fetch", held)
    first: list[tuple[tuple[Decimal, Decimal], str | None] | None] = [None]

    def reader() -> None:
        first[0] = model_catalog.models_dev_price_with_read_time(
            "reflection", BEAM, deadline_seconds=5.0
        )

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        assert started.wait(5)
        waiter = model_catalog.models_dev_price_with_read_time(
            "reflection", BEAM, deadline_seconds=0.2
        )
        assert waiter is None
        assert len(live_models_dev.reads) == 1, (
            "the waiting caller started its own read"
        )
        assert not _failure_marker_path(tmp_path).exists(), (
            "a caller that only waited marked a failure"
        )
    finally:
        release.set()
        thread.join(10)
    assert first[0] is not None
    assert first[0][0] == _PRICED
    assert len(live_models_dev.reads) == 1


# --- Round-3: a timed-out read marks a failure only when it ran out of the full
# configured deadline, and a fresh kept copy prices a call whatever its deadline.
#
# Failure modes:
#   S4  a call with little time left cuts its own read short and marks a failure,
#       so every call stops reading models.dev for the retry interval.
#   S5  a zero deadline leaves a call unknown though a fresh kept copy could price it.


@pytest.mark.parametrize(
    ("cap", "marked"),
    [(3.0, False), (0.2, True)],
    ids=["shorter-than-configured", "configured-deadline"],
)
def test_only_a_read_cut_at_the_full_configured_deadline_marks_a_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    live_models_dev: _ModelsDevServer,
    cap: float,
    marked: bool,
) -> None:
    settings = model_catalog.catalog_config()
    patched = {
        **settings,
        "models_dev": {**settings["models_dev"], "call_read_deadline_seconds": cap},
    }
    monkeypatch.setattr(model_catalog, "catalog_config", lambda: patched)
    started = threading.Event()
    release = threading.Event()

    def held(url: str, timeout: float) -> bytes:
        live_models_dev.reads.append(url)
        started.set()
        release.wait(10)
        return json.dumps(_models_dev_document(BEAM, (0.3, 1.0))).encode()

    monkeypatch.setattr(model_catalog, "_fetch", held)
    marker = _failure_marker_path(tmp_path)
    try:
        found = model_catalog.models_dev_price_with_read_time(
            "reflection", BEAM, deadline_seconds=0.2
        )
        assert found is None
        assert started.is_set(), "the call's own read never began"
        assert marker.exists() is marked, (
            "a read cut short of the configured deadline marked a failure"
            if not marked
            else "a read cut at the configured deadline marked nothing"
        )
    finally:
        release.set()
        _join_price_readers()
    if not marked:
        assert not marker.exists()
        again = model_catalog.models_dev_price_with_read_time(
            "reflection", BEAM, deadline_seconds=3.0
        )
        assert again is not None
        assert again[0] == _PRICED
        assert len(live_models_dev.reads) == 1


def test_a_zero_deadline_still_prices_from_a_fresh_kept_copy(
    tmp_path: Path, live_models_dev: _ModelsDevServer
) -> None:
    cache = _models_dev_cache(tmp_path)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(_models_dev_document(BEAM, (0.3, 1.0))), encoding="utf-8"
    )
    found = model_catalog.models_dev_price_with_read_time(
        "reflection", BEAM, deadline_seconds=0.0
    )
    assert found is not None, "a fresh kept copy did not price a zero-deadline call"
    assert found[0] == _PRICED
    assert found[1] == datetime.fromtimestamp(cache.stat().st_mtime, UTC).isoformat()
    assert live_models_dev.reads == []
