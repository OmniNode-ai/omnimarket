# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15868 -- BaseProjectionRunner.run()'s retry-loop guard never enters.

Root cause (source-verified, ``omnimarket@85da1218``):

    ``__init__`` (``runner.py:409``) sets ``self._running = False``.
    ``run()``'s while-guard (``runner.py:736``) reads
    ``while attempts < MAX_RETRY_ATTEMPTS and self._running is not False:``.
    ``False is not False`` is always ``False`` (Python singleton identity), so
    the guard is False on the very first evaluation -- before a single
    ``AIOKafkaConsumer`` is constructed. The loop body (which contains the
    only ``AIOKafkaConsumer(...)`` construction, ``await
    self._consumer.start()``, and ``self._running = True`` on success) never
    executes, for any subclass, on any invocation. ``run()`` falls straight
    through to ``logger.error("Consumer failed after %d retries", ...)`` and
    returns normally (exit code 0) -- observed live as
    ``CrashLoopBackOff`` on every ``BaseProjectionRunner`` subclass deployed
    as a standalone k8s process (OMN-15800, deploy run 31493137810).

RED before the fix (recorded 2026-08-11): the two tests in this file assert
observable proof the loop body actually executes -- an
``AIOKafkaConsumer.start()`` call happens at all, a real ``await
asyncio.sleep()`` backoff is awaited, and ``self._running`` transitions to
``True`` on a successful connect. Against pre-fix code both fail: zero calls,
zero sleeps, ``self._running`` stays ``False`` forever -- not because the test
setup is wrong, but because the while-guard prevents entry.

GREEN after the fix: the recommended fix (ticket body) tracks
"shutdown requested" as a dedicated sentinel (``self._shutdown_requested``)
separate from the readiness signal ``self._running``, so the guard no longer
self-defeats on a freshly constructed runner.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.projection.runner import (
    RETRY_BASE_DELAY,
    BaseProjectionRunner,
    MessageMeta,
    ModelProjectionRuntimeBinding,
)


class _ControlFlowStop(BaseException):
    """Raised by test fakes to unwind ``run()`` at a precisely observed point.

    Subclasses ``BaseException`` directly (not ``Exception``) so
    ``BaseProjectionRunner.run()``'s ``except Exception`` retry handler does
    not swallow it -- it propagates straight out of ``run()``. This is the
    same idiom ``test_projection_kafka_auth_kwargs_omn15816.py`` uses
    (``_StopAfterConstruct``); named distinctly here since it is raised from
    a different site (the patched ``asyncio.sleep`` / the consumer's
    ``__anext__``, not ``start()``).
    """


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


class _FakeConsumerAlwaysFails:
    """``start()`` always raises -- proves the retry loop is entered at all."""

    start_call_count = 0

    def __init__(self, *topics: str, **kwargs: Any) -> None:
        pass

    async def start(self) -> None:
        type(self).start_call_count += 1
        raise ConnectionError("simulated broker unavailable")


class _FakeConsumerFailsThenSucceeds:
    """``start()`` raises ``fail_count`` times, then succeeds.

    After a successful ``start()``, iteration begins; ``__anext__`` raises
    ``_ControlFlowStop`` immediately so the test observes ``self._running``
    at the exact point of a successful connect, without needing a real
    message or a real Kafka broker.
    """

    start_call_count = 0
    fail_count = 2

    def __init__(self, *topics: str, **kwargs: Any) -> None:
        pass

    async def start(self) -> None:
        type(self).start_call_count += 1
        if type(self).start_call_count <= type(self).fail_count:
            raise ConnectionError("simulated broker unavailable")

    async def stop(self) -> None:
        return None

    def __aiter__(self) -> _FakeConsumerFailsThenSucceeds:
        return self

    async def __anext__(self) -> Any:
        raise _ControlFlowStop()


@pytest.mark.unit
async def test_run_retry_loop_actually_enters_and_retries_omn15868() -> None:
    """AC1 (RED-first): a failing ``start()`` proves the loop retries.

    Asserts the loop body actually executed (``start()`` was called at all)
    and a real ``await asyncio.sleep()`` backoff was awaited with the
    expected exponential-backoff delay for the first retry -- not just that
    the final "Consumer failed after N retries" log line appears (that line
    prints unconditionally today with zero loop iterations, which is exactly
    the bug).
    """
    runner = _make_runner()
    _FakeConsumerAlwaysFails.start_call_count = 0

    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        raise _ControlFlowStop()

    with (
        patch(
            "omnimarket.projection.runner.AIOKafkaConsumer",
            _FakeConsumerAlwaysFails,
        ),
        patch("omnimarket.projection.runner.asyncio.sleep", _fake_sleep),
        pytest.raises(_ControlFlowStop),
    ):
        await runner.run()

    assert _FakeConsumerAlwaysFails.start_call_count >= 1, (
        "AIOKafkaConsumer.start() was never attempted -- the retry loop "
        "body never executed (this is the OMN-15868 bug: the while-guard "
        "`self._running is not False` is False on a freshly constructed "
        "runner, so the loop is skipped entirely)"
    )
    assert len(sleep_calls) >= 1, (
        "no real asyncio.sleep() backoff was ever awaited -- the except "
        "block that retries was never reached"
    )
    assert sleep_calls[0] == pytest.approx(RETRY_BASE_DELAY * (2**1))


@pytest.mark.unit
async def test_run_retry_loop_recovers_and_sets_running_true_omn15868() -> None:
    """AC2 (GREEN): fail twice then succeed -> self._running becomes True.

    Proves the loop exits via the success path (consumer construction
    succeeds and message iteration begins) rather than falling through to
    the retry-exhaustion ``logger.error("Consumer failed after %d
    retries", ...)`` path -- the ``_ControlFlowStop`` raised from
    ``__anext__`` only fires once iteration has begun, which is only
    reachable after ``self._running = True`` is set on a successful
    ``start()``.
    """
    runner = _make_runner()
    _FakeConsumerFailsThenSucceeds.start_call_count = 0
    _FakeConsumerFailsThenSucceeds.fail_count = 2

    async def _fake_sleep(delay: float) -> None:
        return None

    with (
        patch(
            "omnimarket.projection.runner.AIOKafkaConsumer",
            _FakeConsumerFailsThenSucceeds,
        ),
        patch("omnimarket.projection.runner.asyncio.sleep", _fake_sleep),
        pytest.raises(_ControlFlowStop),
    ):
        await runner.run()

    assert _FakeConsumerFailsThenSucceeds.start_call_count == 3, (
        "expected 2 failed start() calls followed by a 3rd successful one"
    )
    assert runner._running is True, (
        "self._running must transition to True after a successful "
        "start() -- it never does today because the while-guard prevents "
        "the loop (and therefore this assignment) from ever executing"
    )
