"""Protocol-conformance tests for node_pr_lifecycle_orchestrator sub-handlers.

For every real sub-handler registered by the orchestrator, assert that its
``handle()`` signature matches the corresponding protocol's declared signature.

These tests MUST fail before the OMN-9234 fix is applied (protocol signatures
drifted from the real handlers) and pass after.

Related:
    - OMN-9234: Fix protocol-signature drift in node_pr_lifecycle_orchestrator sub-handlers
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
    ProtocolFixHandler,
    ProtocolInventoryHandler,
    ProtocolMergeHandler,
    ProtocolStateReducerHandler,
    ProtocolTriageHandler,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sig_params(fn: Any) -> dict[str, inspect.Parameter]:
    """Return the parameter dict for a callable, excluding 'self'."""
    sig = inspect.signature(fn)
    return {k: v for k, v in sig.parameters.items() if k != "self"}


# ---------------------------------------------------------------------------
# Protocol stub implementations that mirror the real handler signatures
# (used to verify the protocols themselves are correctly declared)
# ---------------------------------------------------------------------------


class _ConformingInventory:
    """Matches HandlerPrLifecycleInventory.handle(input_model) exactly."""

    def handle(self, input_model: Any) -> Any:
        return None


class _ConformingTriage:
    """Matches HandlerPrLifecycleTriage.handle(correlation_id, prs) exactly."""

    async def handle(self, correlation_id: Any, prs: Any) -> Any:
        return None


class _ConformingReducer:
    """Matches HandlerPrLifecycleStateReducer.handle(*args, **kwargs) exactly."""

    async def handle(self, *args: Any, **kwargs: Any) -> Any:
        return None


class _ConformingMerge:
    """Matches HandlerPrLifecycleMerge.handle(command) exactly."""

    async def handle(self, command: Any) -> Any:
        return None


class _ConformingFix:
    """Matches HandlerPrLifecycleFix.handle(command) exactly."""

    async def handle(self, command: Any) -> Any:
        return None


# ---------------------------------------------------------------------------
# Non-conforming implementations (pre-fix drift shapes — must NOT pass isinstance)
# These represent the old protocol signatures that drifted from real handlers.
# ---------------------------------------------------------------------------


class _DriftingInventory:
    """Old (drifted) inventory signature: keyword-only args instead of input_model."""

    async def handle(
        self, *, correlation_id: Any, repos: Any, dry_run: bool = False
    ) -> Any:
        return None


class _DriftingTriage:
    """Old (drifted) triage signature: keyword-only correlation_id + prs."""

    async def handle(self, *, correlation_id: Any, prs: Any) -> Any:
        return None


class _DriftingMerge:
    """Old (drifted) merge signature: keyword-only args instead of command."""

    async def handle(
        self,
        *,
        correlation_id: Any,
        prs_to_merge: Any,
        dry_run: bool = False,
    ) -> Any:
        return None


class _DriftingFix:
    """Old (drifted) fix signature: keyword-only args instead of command."""

    async def handle(
        self,
        *,
        correlation_id: Any,
        prs_to_fix: Any,
        dry_run: bool = False,
        enable_admin_merge_fallback: bool = True,
        admin_fallback_threshold_minutes: int = 30,
    ) -> Any:
        return None


# ---------------------------------------------------------------------------
# Tests: conforming implementations satisfy protocols
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_conforming_inventory_satisfies_protocol() -> None:
    """A handler with handle(self, input_model) satisfies ProtocolInventoryHandler."""
    handler = _ConformingInventory()
    assert isinstance(handler, ProtocolInventoryHandler), (
        "ProtocolInventoryHandler must accept handle(self, input_model) shape"
    )


@pytest.mark.unit
def test_conforming_triage_satisfies_protocol() -> None:
    """A handler with handle(self, correlation_id, prs) satisfies ProtocolTriageHandler."""
    handler = _ConformingTriage()
    assert isinstance(handler, ProtocolTriageHandler), (
        "ProtocolTriageHandler must accept handle(self, correlation_id, prs) shape"
    )


@pytest.mark.unit
def test_conforming_reducer_satisfies_protocol() -> None:
    """A handler with handle(self, *args, **kwargs) satisfies ProtocolStateReducerHandler."""
    handler = _ConformingReducer()
    assert isinstance(handler, ProtocolStateReducerHandler), (
        "ProtocolStateReducerHandler must accept handle(self, *args, **kwargs) shape"
    )


@pytest.mark.unit
def test_conforming_merge_satisfies_protocol() -> None:
    """A handler with handle(self, command) satisfies ProtocolMergeHandler."""
    handler = _ConformingMerge()
    assert isinstance(handler, ProtocolMergeHandler), (
        "ProtocolMergeHandler must accept handle(self, command) shape"
    )


@pytest.mark.unit
def test_conforming_fix_satisfies_protocol() -> None:
    """A handler with handle(self, command) satisfies ProtocolFixHandler."""
    handler = _ConformingFix()
    assert isinstance(handler, ProtocolFixHandler), (
        "ProtocolFixHandler must accept handle(self, command) shape"
    )


# ---------------------------------------------------------------------------
# Tests: real sub-handlers conform to their protocols (import-guarded)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_real_inventory_handler_conforms_to_protocol() -> None:
    """HandlerPrLifecycleInventory.handle() must conform to ProtocolInventoryHandler."""
    try:
        from omnimarket.nodes.node_pr_lifecycle_inventory_compute.handlers.handler_pr_lifecycle_inventory import (
            HandlerPrLifecycleInventory,
        )
    except ImportError:
        pytest.skip("HandlerPrLifecycleInventory not importable in this environment")

    handler = HandlerPrLifecycleInventory()
    assert isinstance(handler, ProtocolInventoryHandler), (
        f"{type(handler).__name__} does not satisfy ProtocolInventoryHandler — "
        "handle() must accept a single positional input_model argument"
    )
    params = _sig_params(handler.handle)
    assert "input_model" in params, (
        f"HandlerPrLifecycleInventory.handle() must have 'input_model' parameter, "
        f"got: {list(params.keys())}"
    )


@pytest.mark.unit
def test_real_triage_handler_conforms_to_protocol() -> None:
    """HandlerPrLifecycleTriage.handle() must conform to ProtocolTriageHandler."""
    try:
        from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_pr_lifecycle_triage import (
            HandlerPrLifecycleTriage,
        )
    except ImportError:
        pytest.skip("HandlerPrLifecycleTriage not importable in this environment")

    handler = HandlerPrLifecycleTriage()
    assert isinstance(handler, ProtocolTriageHandler), (
        f"{type(handler).__name__} does not satisfy ProtocolTriageHandler — "
        "handle() must accept (correlation_id, prs) positional args"
    )
    params = _sig_params(handler.handle)
    param_names = list(params.keys())
    assert param_names[:2] == ["correlation_id", "prs"], (
        f"HandlerPrLifecycleTriage.handle() must have parameters "
        f"[correlation_id, prs, ...], got: {param_names}"
    )


@pytest.mark.unit
def test_real_reducer_handler_conforms_to_protocol() -> None:
    """HandlerPrLifecycleStateReducer.handle() must conform to ProtocolStateReducerHandler."""
    try:
        from omnimarket.nodes.node_pr_lifecycle_state_reducer.handlers.handler_pr_lifecycle_state_reducer import (
            HandlerPrLifecycleStateReducer,
        )
    except ImportError:
        pytest.skip("HandlerPrLifecycleStateReducer not importable in this environment")

    handler = HandlerPrLifecycleStateReducer()
    assert isinstance(handler, ProtocolStateReducerHandler), (
        f"{type(handler).__name__} does not satisfy ProtocolStateReducerHandler — "
        "handle() must accept *args/**kwargs"
    )
    # Reducer uses *args/**kwargs; just verify handle is callable
    assert callable(handler.handle), (
        "HandlerPrLifecycleStateReducer.handle must be callable"
    )


@pytest.mark.unit
def test_real_merge_handler_conforms_to_protocol() -> None:
    """HandlerPrLifecycleMerge.handle() must conform to ProtocolMergeHandler."""
    try:
        from omnimarket.nodes.node_pr_lifecycle_merge_effect.handlers.handler_pr_lifecycle_merge import (
            HandlerPrLifecycleMerge,
        )
    except ImportError:
        pytest.skip("HandlerPrLifecycleMerge not importable in this environment")

    handler = HandlerPrLifecycleMerge()
    assert isinstance(handler, ProtocolMergeHandler), (
        f"{type(handler).__name__} does not satisfy ProtocolMergeHandler — "
        "handle() must accept a single positional command argument"
    )
    params = _sig_params(handler.handle)
    assert "command" in params, (
        f"HandlerPrLifecycleMerge.handle() must have 'command' parameter, "
        f"got: {list(params.keys())}"
    )


@pytest.mark.unit
def test_real_fix_handler_conforms_to_protocol() -> None:
    """HandlerPrLifecycleFix.handle() must conform to ProtocolFixHandler."""
    try:
        from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix import (
            HandlerPrLifecycleFix,
        )
    except ImportError:
        pytest.skip("HandlerPrLifecycleFix not importable in this environment")

    handler = HandlerPrLifecycleFix()
    assert isinstance(handler, ProtocolFixHandler), (
        f"{type(handler).__name__} does not satisfy ProtocolFixHandler — "
        "handle() must accept a single positional command argument"
    )
    params = _sig_params(handler.handle)
    assert "command" in params, (
        f"HandlerPrLifecycleFix.handle() must have 'command' parameter, "
        f"got: {list(params.keys())}"
    )


# ---------------------------------------------------------------------------
# Tests: negative registration — drifting stubs must be rejected by
# HandlerPrLifecycleOrchestrator._check_protocol_conformance()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_registration_rejects_drifting_inventory() -> None:
    """_check_protocol_conformance rejects a keyword-only inventory.handle."""
    from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
        HandlerPrLifecycleOrchestrator,
    )

    # _DriftingInventory is async (matching old drifted shape) while the
    # protocol is sync; the async/sync check fires first and is itself a
    # valid drift rejection.
    with pytest.raises(TypeError, match=r"drifted|KEYWORD_ONLY|input_model|async|sync"):
        HandlerPrLifecycleOrchestrator._check_protocol_conformance(
            _DriftingInventory(), ProtocolInventoryHandler, "inventory"
        )


@pytest.mark.unit
def test_registration_rejects_drifting_triage() -> None:
    """_check_protocol_conformance rejects a keyword-only triage.handle."""
    from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
        HandlerPrLifecycleOrchestrator,
    )

    with pytest.raises(TypeError, match=r"drifted|KEYWORD_ONLY|correlation_id|prs"):
        HandlerPrLifecycleOrchestrator._check_protocol_conformance(
            _DriftingTriage(), ProtocolTriageHandler, "triage"
        )


@pytest.mark.unit
def test_registration_rejects_drifting_merge() -> None:
    """_check_protocol_conformance rejects a keyword-only merge.handle."""
    from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
        HandlerPrLifecycleOrchestrator,
    )

    with pytest.raises(TypeError, match=r"drifted|KEYWORD_ONLY|command"):
        HandlerPrLifecycleOrchestrator._check_protocol_conformance(
            _DriftingMerge(), ProtocolMergeHandler, "merge"
        )


@pytest.mark.unit
def test_registration_rejects_drifting_fix() -> None:
    """_check_protocol_conformance rejects a keyword-only fix.handle."""
    from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
        HandlerPrLifecycleOrchestrator,
    )

    with pytest.raises(TypeError, match=r"drifted|KEYWORD_ONLY|command"):
        HandlerPrLifecycleOrchestrator._check_protocol_conformance(
            _DriftingFix(), ProtocolFixHandler, "fix"
        )


@pytest.mark.unit
def test_registration_rejects_handler_without_handle_method() -> None:
    """_check_protocol_conformance rejects a handler missing 'handle' entirely."""
    from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
        HandlerPrLifecycleOrchestrator,
    )

    class _NoHandle:
        pass

    with pytest.raises(TypeError, match=r"does not conform|missing required 'handle'"):
        HandlerPrLifecycleOrchestrator._check_protocol_conformance(
            _NoHandle(), ProtocolInventoryHandler, "inventory"
        )


@pytest.mark.unit
def test_registration_rejects_sync_handler_for_async_protocol() -> None:
    """_check_protocol_conformance rejects sync handle() for async protocol (merge)."""
    from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
        HandlerPrLifecycleOrchestrator,
    )

    class _SyncMerge:
        # ProtocolMergeHandler declares async handle(command) — sync here drifts.
        def handle(self, command: Any) -> Any:
            return None

    with pytest.raises(TypeError, match=r"async|sync"):
        HandlerPrLifecycleOrchestrator._check_protocol_conformance(
            _SyncMerge(), ProtocolMergeHandler, "merge"
        )


@pytest.mark.unit
def test_registration_rejects_async_handler_for_sync_protocol() -> None:
    """_check_protocol_conformance rejects async handle() for sync protocol (inventory)."""
    from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
        HandlerPrLifecycleOrchestrator,
    )

    class _AsyncInventory:
        # ProtocolInventoryHandler declares sync handle(input_model) — async drifts.
        async def handle(self, input_model: Any) -> Any:
            return None

    with pytest.raises(TypeError, match=r"async|sync"):
        HandlerPrLifecycleOrchestrator._check_protocol_conformance(
            _AsyncInventory(), ProtocolInventoryHandler, "inventory"
        )


@pytest.mark.unit
def test_registration_rejects_extra_required_parameters() -> None:
    """_check_protocol_conformance rejects extra required handler parameters.

    A handler declaring additional positional/keyword parameters without
    defaults cannot be called via the protocol contract and must fail at
    registration, not dispatch.
    """
    from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
        HandlerPrLifecycleOrchestrator,
    )

    class _ExtraRequired:
        # ProtocolMergeHandler declares handle(command); this adds a required
        # 'token' parameter that the orchestrator never supplies.
        async def handle(self, command: Any, token: str) -> Any:
            return None

    with pytest.raises(TypeError, match=r"required parameter|extra|token"):
        HandlerPrLifecycleOrchestrator._check_protocol_conformance(
            _ExtraRequired(), ProtocolMergeHandler, "merge"
        )


@pytest.mark.unit
def test_registration_accepts_extra_parameters_with_defaults() -> None:
    """_check_protocol_conformance accepts extra handler parameters if they have defaults.

    Handlers may define optional parameters beyond the protocol contract as
    long as they don't break the orchestrator's call pattern (single
    positional arg).
    """
    from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
        HandlerPrLifecycleOrchestrator,
    )

    class _ExtraOptional:
        async def handle(self, command: Any, retries: int = 3) -> Any:
            return None

    # Should not raise.
    HandlerPrLifecycleOrchestrator._check_protocol_conformance(
        _ExtraOptional(), ProtocolMergeHandler, "merge"
    )


# ---------------------------------------------------------------------------
# Tests: signature comparison between protocol and real handlers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_protocol_inventory_handle_param_count() -> None:
    """ProtocolInventoryHandler.handle must have exactly 1 non-self parameter."""
    proto_params = _sig_params(ProtocolInventoryHandler.handle)
    assert len(proto_params) == 1, (
        f"ProtocolInventoryHandler.handle must have 1 parameter (input_model), "
        f"got {len(proto_params)}: {list(proto_params.keys())}"
    )
    assert "input_model" in proto_params, (
        f"ProtocolInventoryHandler.handle must declare 'input_model', "
        f"got: {list(proto_params.keys())}"
    )


@pytest.mark.unit
def test_protocol_triage_handle_positional_params() -> None:
    """ProtocolTriageHandler.handle must have (correlation_id, prs) positional params."""
    proto_params = _sig_params(ProtocolTriageHandler.handle)
    names = list(proto_params.keys())
    assert names[:2] == ["correlation_id", "prs"], (
        f"ProtocolTriageHandler.handle must start with [correlation_id, prs], "
        f"got: {names}"
    )


@pytest.mark.unit
def test_protocol_merge_handle_command_param() -> None:
    """ProtocolMergeHandler.handle must have a single 'command' parameter."""
    proto_params = _sig_params(ProtocolMergeHandler.handle)
    assert "command" in proto_params, (
        f"ProtocolMergeHandler.handle must have 'command' parameter, "
        f"got: {list(proto_params.keys())}"
    )


@pytest.mark.unit
def test_protocol_fix_handle_command_param() -> None:
    """ProtocolFixHandler.handle must have a single 'command' parameter."""
    proto_params = _sig_params(ProtocolFixHandler.handle)
    assert "command" in proto_params, (
        f"ProtocolFixHandler.handle must have 'command' parameter, "
        f"got: {list(proto_params.keys())}"
    )
