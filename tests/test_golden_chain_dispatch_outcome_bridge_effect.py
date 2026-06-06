# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden-chain tests for node_dispatch_outcome_bridge_effect.

omniintelligence is a runtime peer dependency that is not installed in the
omnimarket test environment.  All tests here stub the omniintelligence module
tree in sys.modules so that the handler's lazy imports resolve against fakes
rather than the real package.
"""

from __future__ import annotations

import enum
import json
import sys
import types
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# sys.modules stubs for omniintelligence peer dependency
# ---------------------------------------------------------------------------

# We must install the stubs before importing the handler module so the lazy
# `from omniintelligence...` calls inside functions find the fakes.


class _FakeEnumUsageSource(enum.StrEnum):
    MEASURED = "measured"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class _FakeModelCostProvenance:
    def __init__(
        self,
        usage_source: _FakeEnumUsageSource = _FakeEnumUsageSource.UNKNOWN,
        **_: Any,
    ) -> None:
        self.usage_source = usage_source


class _FakeModelCallRecord:
    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeModelInput:
    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeModelOutput:
    def __init__(
        self,
        verdict: str = "PASS",
        quality_score: float | None = None,
        token_cost: int = 0,
        dollars_cost: float = 0.0,
        usage_source: str | None = None,
        estimation_method: str | None = None,
        source_payload_hash: str | None = None,
        evaluated_at: datetime | None = None,
        eval_latency_ms: int = 0,
        **_: Any,
    ) -> None:
        self.verdict = verdict
        self.quality_score = quality_score
        self.token_cost = token_cost
        self.dollars_cost = dollars_cost
        self.usage_source = usage_source
        self.estimation_method = estimation_method
        self.source_payload_hash = source_payload_hash
        self.evaluated_at = evaluated_at or datetime.now(UTC)
        self.eval_latency_ms = eval_latency_ms


def _make_fake_handle_dispatch_outcome(verdict: str = "PASS") -> AsyncMock:
    """Return a mock coroutine that produces a _FakeModelOutput with the given verdict."""
    mock = AsyncMock(return_value=_FakeModelOutput(verdict=verdict))
    return mock


def _install_omniintelligence_stubs(verdict: str = "PASS") -> dict[str, Any]:
    """Inject stub modules for the omniintelligence sub-packages used by the handler.

    Returns the mapping of inserted module names so callers can restore them.
    """
    handle_mock = _make_fake_handle_dispatch_outcome(verdict)

    # Build module hierarchy
    root = types.ModuleType("omniintelligence")
    nodes_pkg = types.ModuleType("omniintelligence.nodes")
    eval_node = types.ModuleType(
        "omniintelligence.nodes.node_dispatch_outcome_eval_effect"
    )
    handlers_pkg = types.ModuleType(
        "omniintelligence.nodes.node_dispatch_outcome_eval_effect.handlers"
    )
    handler_mod = types.ModuleType(
        "omniintelligence.nodes.node_dispatch_outcome_eval_effect.handlers.handler_dispatch_outcome"
    )
    models_pkg = types.ModuleType(
        "omniintelligence.nodes.node_dispatch_outcome_eval_effect.models"
    )
    model_input_mod = types.ModuleType(
        "omniintelligence.nodes.node_dispatch_outcome_eval_effect.models.model_input"
    )
    model_output_mod = types.ModuleType(
        "omniintelligence.nodes.node_dispatch_outcome_eval_effect.models.model_output"
    )

    # Populate symbols
    handler_mod.handle_dispatch_outcome = handle_mock  # type: ignore[attr-defined]
    model_input_mod.EnumUsageSource = _FakeEnumUsageSource  # type: ignore[attr-defined]
    model_input_mod.ModelCallRecord = _FakeModelCallRecord  # type: ignore[attr-defined]
    model_input_mod.ModelCostProvenance = _FakeModelCostProvenance  # type: ignore[attr-defined]
    model_input_mod.ModelInput = _FakeModelInput  # type: ignore[attr-defined]
    model_output_mod.ModelOutput = _FakeModelOutput  # type: ignore[attr-defined]

    stubs: dict[str, Any] = {
        "omniintelligence": root,
        "omniintelligence.nodes": nodes_pkg,
        "omniintelligence.nodes.node_dispatch_outcome_eval_effect": eval_node,
        "omniintelligence.nodes.node_dispatch_outcome_eval_effect.handlers": handlers_pkg,
        "omniintelligence.nodes.node_dispatch_outcome_eval_effect.handlers.handler_dispatch_outcome": handler_mod,
        "omniintelligence.nodes.node_dispatch_outcome_eval_effect.models": models_pkg,
        "omniintelligence.nodes.node_dispatch_outcome_eval_effect.models.model_input": model_input_mod,
        "omniintelligence.nodes.node_dispatch_outcome_eval_effect.models.model_output": model_output_mod,
    }
    # Save originals (likely absent)
    originals = {k: sys.modules.get(k) for k in stubs}
    sys.modules.update(stubs)
    return originals


def _restore_modules(originals: dict[str, Any]) -> None:
    for k, v in originals.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


@pytest.fixture
def omniintelligence_stubs():  # type: ignore[return]
    """Fixture: install omniintelligence stubs before each test, restore after."""
    originals = _install_omniintelligence_stubs("PASS")
    yield
    _restore_modules(originals)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeDb:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append((query, *args))
        return "INSERT 0 1"


def _completed_event(
    task_id: str = "t1",
    dispatch_id: str = "d1",
    status: str = "completed",
    token_cost: int = 1000,
    dollars_cost: float = 0.01,
    ticket_id: str | None = "OMN-99999",
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "dispatch_id": dispatch_id,
        "ticket_id": ticket_id,
        "status": status,
        "token_cost": token_cost,
        "dollars_cost": dollars_cost,
        "model_calls": [],
        "cost_provenance": {"usage_source": "unknown"},
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_event_pass_verdict_inserts_row() -> None:
    originals = _install_omniintelligence_stubs("PASS")
    try:
        from omnimarket.nodes.node_dispatch_outcome_bridge_effect.handlers.handler_bridge import (
            SQL_UPSERT_DISPATCH_EVAL_RESULT,
            process_event,
        )

        db = _FakeDb()
        ok = await process_event(_completed_event(status="completed"), db, None)

        assert ok is True
        assert len(db.calls) == 1
        query, task_id, dispatch_id, *rest = db.calls[0]
        assert SQL_UPSERT_DISPATCH_EVAL_RESULT.strip() in query.strip()
        assert task_id == "t1"
        assert dispatch_id == "d1"
        verdict = rest[1]
        assert verdict == "PASS"
    finally:
        _restore_modules(originals)


@pytest.mark.asyncio
async def test_process_event_failed_status_writes_fail_verdict() -> None:
    originals = _install_omniintelligence_stubs("FAIL")
    try:
        from omnimarket.nodes.node_dispatch_outcome_bridge_effect.handlers.handler_bridge import (
            process_event,
        )

        db = _FakeDb()
        ok = await process_event(_completed_event(status="failed"), db, None)

        assert ok is True
        assert len(db.calls) == 1
        verdict = db.calls[0][4]
        assert verdict == "FAIL"
    finally:
        _restore_modules(originals)


@pytest.mark.asyncio
async def test_process_event_error_status_writes_error_verdict() -> None:
    originals = _install_omniintelligence_stubs("ERROR")
    try:
        from omnimarket.nodes.node_dispatch_outcome_bridge_effect.handlers.handler_bridge import (
            process_event,
        )

        db = _FakeDb()
        ok = await process_event(_completed_event(status="error"), db, None)

        assert ok is True
        verdict = db.calls[0][4]
        assert verdict == "ERROR"
    finally:
        _restore_modules(originals)


@pytest.mark.asyncio
async def test_process_event_publishes_to_kafka_when_producer_present() -> None:
    originals = _install_omniintelligence_stubs("PASS")
    try:
        from omnimarket.nodes.node_dispatch_outcome_bridge_effect.handlers.handler_bridge import (
            process_event,
        )

        db = _FakeDb()
        producer = AsyncMock()
        producer.send_and_wait = AsyncMock()

        ok = await process_event(_completed_event(), db, producer)

        assert ok is True
        producer.send_and_wait.assert_awaited_once()
        call_args = producer.send_and_wait.call_args
        topic = call_args[0][0]
        assert topic == "onex.evt.omniintelligence.dispatch-outcome-evaluated.v1"

        raw_value = call_args[1]["value"]
        published = json.loads(raw_value.decode())
        assert published["task_id"] == "t1"
        assert published["dispatch_id"] == "d1"
        assert published["verdict"] == "PASS"
    finally:
        _restore_modules(originals)


@pytest.mark.asyncio
async def test_process_event_skips_publish_when_no_producer() -> None:
    originals = _install_omniintelligence_stubs("PASS")
    try:
        from omnimarket.nodes.node_dispatch_outcome_bridge_effect.handlers.handler_bridge import (
            process_event,
        )

        db = _FakeDb()
        ok = await process_event(_completed_event(), db, None)
        assert ok is True
        assert len(db.calls) == 1
    finally:
        _restore_modules(originals)


@pytest.mark.asyncio
async def test_process_event_db_error_returns_false() -> None:
    originals = _install_omniintelligence_stubs("PASS")
    try:
        from omnimarket.nodes.node_dispatch_outcome_bridge_effect.handlers.handler_bridge import (
            process_event,
        )

        class _ErrorDb:
            async def execute(self, *args: Any) -> str:
                raise RuntimeError("db connection lost")

        ok = await process_event(_completed_event(), _ErrorDb(), None)
        assert ok is False
    finally:
        _restore_modules(originals)


@pytest.mark.asyncio
async def test_process_event_malformed_payload_returns_false() -> None:
    originals = _install_omniintelligence_stubs("PASS")
    try:
        from omnimarket.nodes.node_dispatch_outcome_bridge_effect.handlers.handler_bridge import (
            process_event,
        )

        ok = await process_event(
            {"task_id": "", "dispatch_id": "", "status": ""}, _FakeDb(), None
        )
        assert ok is False
    finally:
        _restore_modules(originals)


@pytest.mark.asyncio
async def test_process_event_handle_dispatch_outcome_called_once() -> None:
    """handle_dispatch_outcome must be called exactly once per process_event invocation."""
    originals = _install_omniintelligence_stubs("PASS")
    try:
        # Grab the mock we installed so we can assert call count
        handler_mod = sys.modules[
            "omniintelligence.nodes.node_dispatch_outcome_eval_effect.handlers.handler_dispatch_outcome"
        ]
        mock_eval: AsyncMock = handler_mod.handle_dispatch_outcome  # type: ignore[attr-defined]

        from omnimarket.nodes.node_dispatch_outcome_bridge_effect.handlers.handler_bridge import (
            process_event,
        )

        db = _FakeDb()
        ok = await process_event(_completed_event(), db, None)

        assert ok is True
        mock_eval.assert_awaited_once()
    finally:
        _restore_modules(originals)


@pytest.mark.asyncio
async def test_process_event_handle_dispatch_outcome_raises_returns_false() -> None:
    """If handle_dispatch_outcome raises, process_event returns False (no crash)."""
    originals = _install_omniintelligence_stubs("PASS")
    try:
        handler_mod = sys.modules[
            "omniintelligence.nodes.node_dispatch_outcome_eval_effect.handlers.handler_dispatch_outcome"
        ]
        failing_mock: MagicMock = AsyncMock(side_effect=RuntimeError("eval failed"))
        handler_mod.handle_dispatch_outcome = failing_mock  # type: ignore[attr-defined]

        from omnimarket.nodes.node_dispatch_outcome_bridge_effect.handlers.handler_bridge import (
            process_event,
        )

        ok = await process_event(_completed_event(), _FakeDb(), None)
        assert ok is False
    finally:
        _restore_modules(originals)
