# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15816 — projection-api's aiokafka clients must carry SASL/IAM auth kwargs.

``omninode_infra#843`` (OMN-15814) fixed the original projection-api crash
(no Kafka bootstrap servers resolved) but exposed a second, deeper defect: the
pod now connects to onex-dev's MSK broker (``security_protocol=SASL_SSL``,
``sasl_mechanism=AWS_MSK_IAM``, port ``:9098`` only -- no plaintext listener)
using aiokafka clients constructed with **zero** auth kwargs. aiokafka
defaults to ``security_protocol="PLAINTEXT"``, so the client opens a TCP
connection the broker's SASL_SSL listener immediately closes -- the observed
live signature (two independent captures of pod
``omnimarket-projection-api-b9c48f87-2jnmh``, ~26-27s after "Waiting for
application startup", ``KafkaConnectionError: Connection at HOST:9098
closed`` for both brokers) discriminated as the proximate cause (ledger
2026-08-10T10:11:53Z [omn15816-projapi-auth]).

Root cause (source-verified, ``omnimarket@f75995cd``):
    * ``SnapshotCache.start()`` (``snapshot_cache.py:313-321``) constructs its
      ``AIOKafkaConsumer`` with only ``bootstrap_servers``/``group_id``/
      ``client_id``/``auto_offset_reset``/``enable_auto_commit``/
      ``value_deserializer`` -- no ``security_protocol``/``sasl_mechanism``.
    * ``BaseProjectionRunner.run()``'s consumer (``runner.py:636``) and
      ``BaseProjectionRunner._ensure_producer()``'s producer
      (``runner.py:437``) have the identical gap.

This is the same anti-pattern class OMN-14155 closed platform-wide in
``omnibase_infra`` on 2026-07-15 (see the healthy reference pattern in
``omnibase_infra/services/observability/agent_actions/consumer.py``, which
spreads ``**build_aiokafka_auth_kwargs_from_env()`` into every aiokafka
client construction). ``snapshot_cache.py`` is new (born with OMN-15800,
2026-08-09/10) -- it postdates that sweep and reintroduces the defect in a
file the sweep never scanned.

RED before the fix (recorded 2026-08-10): every ``*_applies_sasl_auth_kwargs``
test below fails against pre-fix code because none of the three construction
sites spread ``build_aiokafka_auth_kwargs_from_env()`` -- the captured
constructor kwargs never carry ``security_protocol``/``sasl_mechanism`` no
matter what ``KAFKA_SECURITY_PROTOCOL``/``KAFKA_SASL_MECHANISM`` env vars are
set. The ``*_no_env_stays_plaintext`` tests are the paired regression guard:
they prove the local/plaintext default (no env vars set) is unchanged by the
fix -- ``build_aiokafka_auth_kwargs`` returns ``{}`` for
``security_protocol="PLAINTEXT"``, so spreading it into the constructor is a
no-op in that case. They pass both before and after the fix; they exist so a
future change that starts requiring the SASL env vars unconditionally would
be caught here first.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    ModelProjectionRuntimeBinding,
)
from omnimarket.projection.snapshot_cache import SnapshotCache

_SNAPSHOT_TOPIC = "onex.snapshot.projection.test-kafka-auth-omn15816.v1"

_SASL_ENV = {
    "KAFKA_SECURITY_PROTOCOL": "SASL_SSL",
    "KAFKA_SASL_MECHANISM": "AWS_MSK_IAM",
    "KAFKA_MSK_REGION": "us-east-1",
}
_PLAINTEXT_ENV_VARS = (
    "KAFKA_SECURITY_PROTOCOL",
    "KAFKA_SASL_MECHANISM",
    "KAFKA_MSK_REGION",
    "KAFKA_BOOTSTRAP_SERVERS",
)


def _set_sasl_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _SASL_ENV.items():
        monkeypatch.setenv(key, value)


def _clear_kafka_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _PLAINTEXT_ENV_VARS:
        monkeypatch.delenv(key, raising=False)


def _make_cache() -> SnapshotCache:
    exposure = ProjectionTableConfig(
        topic=_SNAPSHOT_TOPIC,
        table="test_table_omn15816",
        columns=("id", "value"),
        bus_backed=True,
        key_columns=("id",),
        limit=100,
    )
    return SnapshotCache(
        {_SNAPSHOT_TOPIC: exposure},
        bootstrap_servers="unused:9098",
        # Explicit override (OMN-15840): this test exercises SASL auth kwargs,
        # not the default group-id derivation, which requires ONEX_ENVIRONMENT.
        group_id="test-kafka-auth-kwargs-group",
    )


class _FakeAsyncClient:
    """Records constructor kwargs; ``start()`` succeeds and does nothing else."""

    def __init__(self, *topics: str, **kwargs: Any) -> None:
        self.topics = topics
        self.kwargs = kwargs

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


class _StopAfterConstruct(BaseException):
    """Raised by a fake client's ``start()`` to unwind ``run()`` immediately.

    Subclasses ``BaseException`` directly (not ``Exception``) so
    ``BaseProjectionRunner.run()``'s ``except Exception`` retry handler does
    not swallow it -- it propagates straight out of ``run()``, skipping the
    exponential-backoff retry loop entirely so the test doesn't sleep.
    """


class _FakeConsumerStopAfterConstruct:
    captured_kwargs: dict[str, Any] | None = None
    captured_topics: tuple[str, ...] | None = None

    def __init__(self, *topics: str, **kwargs: Any) -> None:
        type(self).captured_kwargs = kwargs
        type(self).captured_topics = topics

    async def start(self) -> None:
        raise _StopAfterConstruct()


class _StubRunner(BaseProjectionRunner):
    @property
    def topics(self) -> list[str]:
        return ["some.inbound.topic.v1"]

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        return True


def _make_runner() -> BaseProjectionRunner:
    binding = ModelProjectionRuntimeBinding(
        kafka_bootstrap_servers="b-1.example.kafka.us-east-1.amazonaws.com:9098,"
        "b-2.example.kafka.us-east-1.amazonaws.com:9098",
        database_url="postgresql://stub/stub",  # type: ignore[arg-type]
    )
    runner = _StubRunner(runtime_binding=binding)
    mock_db = MagicMock(spec=AsyncpgAdapter)
    mock_db.connect = AsyncMock()
    mock_db.close = AsyncMock()
    runner._db = mock_db
    return runner


# --- SnapshotCache.start() -- consumer construction site ------------------


@pytest.mark.unit
async def test_snapshot_cache_consumer_applies_sasl_auth_kwargs_omn15816(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_sasl_env(monkeypatch)
    cache = _make_cache()

    with patch(
        "omnimarket.projection.snapshot_cache.AIOKafkaConsumer",
        _FakeAsyncClient,
    ):
        await cache.start()
        task = cache._consume_task
        assert task is not None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    consumer = cache._consumer
    assert isinstance(consumer, _FakeAsyncClient)
    assert consumer.kwargs.get("security_protocol") == "SASL_SSL"
    assert consumer.kwargs.get("sasl_mechanism") == "OAUTHBEARER"
    assert "sasl_oauth_token_provider" in consumer.kwargs


@pytest.mark.unit
async def test_snapshot_cache_consumer_no_env_stays_plaintext_omn15816(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_kafka_env(monkeypatch)
    cache = _make_cache()

    with patch(
        "omnimarket.projection.snapshot_cache.AIOKafkaConsumer",
        _FakeAsyncClient,
    ):
        await cache.start()
        task = cache._consume_task
        assert task is not None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    consumer = cache._consumer
    assert isinstance(consumer, _FakeAsyncClient)
    assert "security_protocol" not in consumer.kwargs
    assert "sasl_mechanism" not in consumer.kwargs


# --- BaseProjectionRunner._ensure_producer() -- producer construction site


@pytest.mark.unit
async def test_runner_producer_applies_sasl_auth_kwargs_omn15816(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_sasl_env(monkeypatch)
    runner = _make_runner()

    fake_producer = MagicMock()
    fake_producer.start = AsyncMock()
    fake_producer.stop = AsyncMock()

    with patch(
        "omnimarket.projection.runner.AIOKafkaProducer",
        return_value=fake_producer,
    ) as ctor:
        producer = await runner._ensure_producer()
        assert producer is fake_producer

    _, kwargs = ctor.call_args
    assert kwargs.get("security_protocol") == "SASL_SSL"
    assert kwargs.get("sasl_mechanism") == "OAUTHBEARER"
    assert "sasl_oauth_token_provider" in kwargs


@pytest.mark.unit
async def test_runner_producer_no_env_stays_plaintext_omn15816(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_kafka_env(monkeypatch)
    runner = _make_runner()

    fake_producer = MagicMock()
    fake_producer.start = AsyncMock()
    fake_producer.stop = AsyncMock()

    with patch(
        "omnimarket.projection.runner.AIOKafkaProducer",
        return_value=fake_producer,
    ) as ctor:
        producer = await runner._ensure_producer()
        assert producer is fake_producer

    _, kwargs = ctor.call_args
    assert "security_protocol" not in kwargs
    assert "sasl_mechanism" not in kwargs


# --- BaseProjectionRunner.run() -- consumer construction site -------------


@pytest.mark.unit
async def test_runner_consumer_applies_sasl_auth_kwargs_omn15816(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_sasl_env(monkeypatch)
    runner = _make_runner()
    _FakeConsumerStopAfterConstruct.captured_kwargs = None
    # Separate, out-of-scope defect found while writing this test (filed as
    # its own ticket, not fixed here): run()'s while-loop guard is
    # `attempts < MAX_RETRY_ATTEMPTS and self._running is not False`, but
    # __init__ sets `self._running = False` -- so `self._running is not
    # False` is False on a freshly constructed runner and the loop body
    # (including the AIOKafkaConsumer construction this test targets) never
    # executes at all. Priming `_running = True` here is a test-only
    # workaround so this test can reach and assert on the OMN-15816
    # construction site without also fixing the unrelated defect.
    runner._running = True

    with (
        patch(
            "omnimarket.projection.runner.AIOKafkaConsumer",
            _FakeConsumerStopAfterConstruct,
        ),
        pytest.raises(_StopAfterConstruct),
    ):
        await runner.run()

    kwargs = _FakeConsumerStopAfterConstruct.captured_kwargs
    assert kwargs is not None
    assert kwargs.get("security_protocol") == "SASL_SSL"
    assert kwargs.get("sasl_mechanism") == "OAUTHBEARER"
    assert "sasl_oauth_token_provider" in kwargs


@pytest.mark.unit
async def test_runner_consumer_no_env_stays_plaintext_omn15816(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_kafka_env(monkeypatch)
    runner = _make_runner()
    _FakeConsumerStopAfterConstruct.captured_kwargs = None
    # See the sibling `_applies_sasl_auth_kwargs` test above for why this
    # test-only priming is needed (separate, out-of-scope run()-loop defect).
    runner._running = True

    with (
        patch(
            "omnimarket.projection.runner.AIOKafkaConsumer",
            _FakeConsumerStopAfterConstruct,
        ),
        pytest.raises(_StopAfterConstruct),
    ):
        await runner.run()

    kwargs = _FakeConsumerStopAfterConstruct.captured_kwargs
    assert kwargs is not None
    assert "security_protocol" not in kwargs
    assert "sasl_mechanism" not in kwargs
