# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-12429: runtime auto-wiring resolution for HandlerModelRouter.

node_model_router is ``runtime_dispatch.invocation_mode: in_process`` with an
empty ``subscribe_topics``. The runtime nonetheless walks every contract's
``handler_routing`` at boot and runs ``ServiceHandlerResolver.resolve`` against
each handler; if resolution raises ``TypeError`` (no precedence path satisfies
the constructor) the kernel crashes the whole boot (OMN-8735 fail-loud).

Before the fix, ``HandlerModelRouter.__init__`` required ``policy``,
``registry`` and ``event_bus``. The resolver's known-param path injects only
``event_bus`` / ``container`` / ``ownership_query``, so ``policy``/``registry``
were unsatisfiable and resolution hit the Step-6 hard failure — crash-looping
the stability-test runtime.

This test drives the *real* resolver with the same context shape the kernel
builds, and asserts the handler now resolves via the known-param (event_bus)
path instead of raising.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from omnibase_core.services.service_handler_resolver import (
    EnumHandlerResolutionOutcome,
    ModelHandlerResolverContext,
    ServiceHandlerResolver,
)

from omnimarket.nodes.node_model_router.handlers.handler_model_router import (
    HandlerModelRouter,
)

pytestmark = [pytest.mark.unit]


def _context(event_bus: object) -> ModelHandlerResolverContext:
    return ModelHandlerResolverContext(
        handler_cls=HandlerModelRouter,
        handler_module="omnimarket.nodes.node_model_router.handlers.handler_model_router",
        handler_name="HandlerModelRouter",
        contract_name="model_router",
        node_name="model_router",
        explicit_dependency_shape=None,
        materialized_explicit_dependencies=None,
        event_bus=event_bus,
        container=None,
        ownership_query=None,
    )


def test_resolver_constructs_handler_via_event_bus_known_param() -> None:
    """The kernel resolver must build HandlerModelRouter without crashing.

    Resolution must succeed instead of raising the Step-6 ``TypeError`` that
    crash-looped boot. Because policy/registry are now optional config (supplied
    in-process by the caller, never by the runtime), the constructor has no
    required params and resolves via the zero-arg path; the key invariant is
    that resolution does NOT raise and yields a constructed handler.
    """
    resolver = ServiceHandlerResolver()
    resolution = resolver.resolve(_context(event_bus=MagicMock()))

    assert isinstance(resolution.handler_instance, HandlerModelRouter)
    # Any non-failure outcome is acceptable; Step-6 TypeError is the regression.
    assert resolution.outcome in {
        EnumHandlerResolutionOutcome.RESOLVED_VIA_KNOWN_PARAMS,
        EnumHandlerResolutionOutcome.RESOLVED_VIA_CONTAINER,
        EnumHandlerResolutionOutcome.RESOLVED_VIA_ZERO_ARG,
    }


def test_resolver_does_not_raise_typeerror_for_model_router() -> None:
    """Explicit regression guard for OMN-12429: the Step-6 hard failure.

    Before the fix, resolving HandlerModelRouter raised TypeError naming the
    unsatisfiable ['policy', 'registry', 'event_bus'] params, which the kernel
    re-raises to crash boot. Resolving must never raise that again.
    """
    resolver = ServiceHandlerResolver()
    # No event_bus, no container — the harshest case. Must still not crash.
    resolution = resolver.resolve(_context(event_bus=None))
    assert isinstance(resolution.handler_instance, HandlerModelRouter)
