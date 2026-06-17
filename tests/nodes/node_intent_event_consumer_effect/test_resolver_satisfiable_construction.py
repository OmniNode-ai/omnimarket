# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode.ai Inc.
"""Resolver-satisfiable-construction regression tests for HandlerIntentEventConsumer (OMN-13201).

OMN-12982 (commit ee79972c) routed effect nodes onto the [effects] runtime
profile, so the runtime auto-wiring boot path constructs their handlers with
ZERO constructor args via ServiceHandlerResolver. A handler whose ``__init__``
required an injected ``config`` / ``storage_adapter`` (eagerly building the
Memgraph-backed adapter) raised a resolver ``TypeError`` that crashed the
runtime-effects boot before the :8086 health server bound (the OMN-13201 effects
crash-loop). These tests pin the fix: construction is pure and zero-arg, defaults
are applied, and the storage adapter is resolved lazily — never in ``__init__``.
"""

from __future__ import annotations

import inspect

from omnimarket.nodes.node_intent_event_consumer_effect.handler_intent_event_consumer import (
    HandlerIntentEventConsumer,
)


def test_zero_arg_construction_does_not_raise() -> None:
    """The boot path constructs the handler with no args; this must not raise.

    Before OMN-13201 ``__init__`` required ``config`` and ``storage_adapter`` and
    a zero-arg construction raised ``TypeError``, crashing the effects boot. Now
    construction is pure: config defaults are applied and the storage adapter is
    resolved lazily on first ``handle``.
    """
    handler = HandlerIntentEventConsumer()
    # Config default is applied; storage adapter is NOT eagerly constructed.
    assert handler._config is not None
    assert handler._storage_adapter is None


def test_handle_is_canonical_async_entrypoint() -> None:
    """The handler exposes the canonical ``handle`` coroutine for dispatch."""
    handler = HandlerIntentEventConsumer()
    assert hasattr(handler, "handle")
    assert inspect.iscoroutinefunction(handler.handle)


def test_injected_config_and_adapter_short_circuit_defaults() -> None:
    """Explicitly injected config + adapter are used as-is (tests / explicit wiring)."""
    from unittest.mock import MagicMock

    from omnimarket.nodes.node_intent_event_consumer_effect.models.model_consumer_config import (
        ModelIntentEventConsumerConfig,
    )

    config = ModelIntentEventConsumerConfig()
    adapter = MagicMock()
    handler = HandlerIntentEventConsumer(config=config, storage_adapter=adapter)
    assert handler._config is config
    assert handler._storage_adapter is adapter
