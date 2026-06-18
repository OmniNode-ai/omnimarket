# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode.ai Inc.
"""Resolver-satisfiable-construction regression tests for HandlerKreuzbergParse (OMN-13201).

OMN-12982 (commit ee79972c) routed effect nodes onto the [effects] runtime
profile, so the runtime auto-wiring boot path constructs their handlers with
ZERO constructor args via ServiceHandlerResolver. A handler whose ``__init__``
required an injected ``config`` raised a resolver ``TypeError`` that crashed the
runtime-effects boot before the :8086 health server bound (the OMN-13201 effects
crash-loop). These tests pin the fix: construction is pure and zero-arg, and the
config is resolved lazily at the ``handle`` boundary, never in ``__init__``.
"""

from __future__ import annotations

import inspect

from omnimarket.nodes.node_kreuzberg_parse_effect.handlers.handler_kreuzberg_parse import (
    HandlerKreuzbergParse,
)


def test_zero_arg_construction_does_not_raise() -> None:
    """The boot path constructs the handler with no args; this must not raise.

    Before OMN-13201 ``__init__`` required ``config`` and a zero-arg construction
    raised ``TypeError``, crashing the effects boot. Now construction is pure.
    """
    handler = HandlerKreuzbergParse()
    # Config is NOT resolved in __init__ — it stays None until the handle boundary.
    assert handler._config is None


def test_handle_is_canonical_async_entrypoint() -> None:
    """The handler exposes the canonical ``handle`` coroutine for dispatch."""
    handler = HandlerKreuzbergParse()
    assert hasattr(handler, "handle")
    assert inspect.iscoroutinefunction(handler.handle)


def test_injected_config_short_circuits_lazy_resolution(tmp_path: object) -> None:
    """An explicitly injected config is used as-is, skipping contract resolution."""
    from omnimarket.nodes.node_kreuzberg_parse_effect.models.model_kreuzberg_parse_config import (
        ModelKreuzbergParseConfig,
    )

    config = ModelKreuzbergParseConfig(
        kreuzberg_url="http://localhost:8090",
        text_store_path=str(tmp_path),
        document_root=str(tmp_path),
        parser_version="1.0.0",
    )
    handler = HandlerKreuzbergParse(config=config)
    assert handler._config is config
