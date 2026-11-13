# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The cache handler: lookup and store by (source id, parameter digest, revision) over a key-value backend (OMN-19998).

Golden chain: a lookup on an empty backend is a miss, a store answers stored, the same lookup is a hit with the payload, a changed
revision is a miss again, and an entry past the overlay's time-to-live backstop is a miss. Error chain: a backend that cannot be
reached answers unavailable and never raises, a handler with no backend answers bypass, and a store with no payload is refused by the
model. Failing control: a key that ignores its revision answers a hit for a changed source, the exact failure this key shape prevents.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_source_read_cache_effect.handlers.handler_source_read_cache import (
    HandlerSourceReadCache,
)
from omnimarket.nodes.node_source_read_cache_effect.models.model_source_read_cache import (
    EnumSourceCacheOutcome,
    ModelSourceCacheKey,
    ModelSourceCacheRequest,
    ModelSourceCacheResult,
)
from tests.nodes.node_source_read_cache_effect.support import (
    PREFIX,
    TTL_BACKSTOP,
    Clock,
    DownBackend,
    key,
    memory_reader,
)

pytestmark = pytest.mark.unit


def _handler(clock: Clock) -> HandlerSourceReadCache:
    _reader, backend = memory_reader(clock)
    return HandlerSourceReadCache(backend, PREFIX, TTL_BACKSTOP)


def _lookup(
    handler: HandlerSourceReadCache, revision: str = "r1"
) -> ModelSourceCacheResult:
    return handler.handle(
        ModelSourceCacheRequest(operation="lookup", key=key(revision))
    )


def _store(
    handler: HandlerSourceReadCache, revision: str = "r1", payload: str = "p"
) -> ModelSourceCacheResult:
    return handler.handle(
        ModelSourceCacheRequest(operation="store", key=key(revision), payload=payload)
    )


def test_golden_chain_miss_store_hit_revision_miss_expiry() -> None:
    clock = Clock()
    handler = _handler(clock)
    assert _lookup(handler).outcome == EnumSourceCacheOutcome.MISS
    assert _store(handler).outcome == EnumSourceCacheOutcome.STORED
    hit = _lookup(handler)
    assert (hit.outcome, hit.payload) == (EnumSourceCacheOutcome.HIT, "p")
    assert _lookup(handler, "r2").outcome == EnumSourceCacheOutcome.MISS
    clock.now += TTL_BACKSTOP + 1
    assert _lookup(handler).outcome == EnumSourceCacheOutcome.MISS


def test_the_store_ttl_comes_from_the_request_when_given() -> None:
    clock = Clock()
    handler = _handler(clock)
    handler.handle(
        ModelSourceCacheRequest(
            operation="store", key=key(), payload="p", ttl_seconds=10
        )
    )
    clock.now += 11
    assert _lookup(handler).outcome == EnumSourceCacheOutcome.MISS


def test_error_chain_an_unreachable_backend_answers_unavailable_and_never_raises() -> (
    None
):
    handler = HandlerSourceReadCache(DownBackend(), PREFIX, TTL_BACKSTOP)
    assert _lookup(handler).outcome == EnumSourceCacheOutcome.UNAVAILABLE
    assert _store(handler).outcome == EnumSourceCacheOutcome.UNAVAILABLE


def test_error_chain_no_backend_answers_bypass() -> None:
    handler = HandlerSourceReadCache(None, PREFIX, TTL_BACKSTOP)
    assert _lookup(handler).outcome == EnumSourceCacheOutcome.BYPASS
    assert _store(handler).outcome == EnumSourceCacheOutcome.BYPASS


def test_error_chain_a_store_without_payload_and_a_lookup_with_one_are_refused() -> (
    None
):
    with pytest.raises(ValidationError):
        ModelSourceCacheRequest(operation="store", key=key())
    with pytest.raises(ValidationError):
        ModelSourceCacheRequest(operation="lookup", key=key(), payload="p")


def test_failing_control_a_key_that_ignores_the_revision_serves_a_changed_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _handler(Clock())
    _store(handler, "r1", "old")
    monkeypatch.setattr(
        ModelSourceCacheKey,
        "render",
        lambda self, prefix: f"{prefix}{self.source_id}:{self.params_digest}",
    )
    broken = HandlerSourceReadCache(handler._backend, PREFIX, TTL_BACKSTOP)
    assert _store(broken, "r1", "old").outcome == EnumSourceCacheOutcome.STORED
    assert _lookup(broken, "r2").outcome == EnumSourceCacheOutcome.HIT
    monkeypatch.undo()
    assert _lookup(handler, "r2").outcome == EnumSourceCacheOutcome.MISS


def test_keys_of_different_parameters_or_sources_do_not_collide() -> None:
    a = key("r1", "one").render(PREFIX)
    b = key("r1", "two").render(PREFIX)
    c = ModelSourceCacheKey(
        source_id="one",
        params_digest=ModelSourceCacheKey.digest({"limit": 6}),
        revision="r1",
    ).render(PREFIX)
    assert len({a, b, c}) == 3
