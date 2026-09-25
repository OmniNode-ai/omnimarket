# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local delegate path makes no broker publish, and leaks no coroutine (OMN-19193).

THE DEFECT, observed 2026-09-22 on run ``844cac18-1d4a-4e74-9377-4049a94b8904``.
``LocalDelegationDispatchPort._project_evidence`` calls the SYNC delegation
projection from inside the ``RuntimeLocal`` event loop. The projection writes the
row, then republishes it through ``KafkaSnapshotDeltaPublisher.publish``, which
calls ``asyncio.run(self._publish(...))``. Inside a running loop ``asyncio.run``
raises before it awaits anything, so:

* the ``_publish`` coroutine is created and never awaited (the RuntimeWarning on
  the operator's terminal);
* the row snapshot delta is dropped and the aggregate republish after it never
  runs;
* the port logs "Failed to project local delegation evidence" for a row that
  was in fact written.

WHY THE FIX IS NOT "MAKE IT AWAIT". The publisher takes its broker from ambient
``KAFKA_BOOTSTRAP_SERVERS``. On the operator's machine that names the
stability-test lane's broker, so a publish that worked would put every tier-0
local SQLite row onto a proof lane's snapshot topic. The local port's evidence
store is not what any lane's projection API serves, so the local port declares
that it does not republish, and the publisher itself refuses -- typed, before it
creates a coroutine -- when it is called from a running loop, which its own
docstring already states is outside its contract.

The lane paths are deliberately untouched and pinned here: the kernel seam runs
sync handlers on a worker thread with no running loop, and a handler built with
no injected publisher still resolves the Kafka publisher.

No mock stands in for the store: the local-port test writes through the real
``SqliteDatabaseAdapter`` and reads the row back with ``sqlite3``.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import sqlite3
import warnings
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_result import (
    ModelLlmDelegationCallResult,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.projection import snapshot_publisher as _publisher_module
from omnimarket.projection.snapshot_publisher import (
    KafkaSnapshotDeltaPublisher,
    ModelSnapshotDeltaMessage,
    SnapshotPublishFromRunningLoopError,
)

pytestmark = pytest.mark.unit

#: TEST-NET-1 (RFC 5737). Never routable, so a publish that escaped the guard
#: could not reach a real broker even on a machine on the lab network.
_UNROUTABLE_BROKER = "192.0.2.1:9092"

_PORT_LOGGER = (
    "omnimarket.nodes.node_delegate_skill_orchestrator.ports."
    "port_local_delegation_dispatch"
)


def _result() -> ModelLlmDelegationCallResult:
    return ModelLlmDelegationCallResult(
        request_id="req-omn19193",
        success=True,
        content="an answer",
        tokens_in=11,
        tokens_out=22,
        latency_ms=33,
        actual_cost_usd=Decimal("0"),
        savings_usd=Decimal("0.5"),
    )


def _message() -> ModelSnapshotDeltaMessage:
    return ModelSnapshotDeltaMessage(
        topic="onex.snapshot.projection.delegation.decisions.v1",
        key=b"k",
        value=b"{}",
        headers=(),
    )


def _never_awaited(caught: list[warnings.WarningMessage]) -> list[str]:
    return [
        str(w.message)
        for w in caught
        if issubclass(w.category, RuntimeWarning) and "never awaited" in str(w.message)
    ]


class _PublishSpy:
    """Records every KafkaSnapshotDeltaPublisher.publish call, then defers to it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        original = KafkaSnapshotDeltaPublisher.publish

        def spy(
            inner: KafkaSnapshotDeltaPublisher, message: ModelSnapshotDeltaMessage
        ) -> bool:
            self.calls += 1
            return original(inner, message)

        monkeypatch.setattr(KafkaSnapshotDeltaPublisher, "publish", spy)


class TestTheLocalPortMakesNoBrokerPublish:
    """AC1: the in-loop local projection writes the row and publishes nothing."""

    def test_inside_a_running_loop_the_row_lands_and_no_publish_is_attempted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The operator's shell exports a broker. That must not become a target.
        monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", _UNROUTABLE_BROKER)
        spy = _PublishSpy(monkeypatch)
        db_path = tmp_path / "delegation.sqlite"
        port = LocalDelegationDispatchPort(evidence_db_path=db_path)
        correlation_id = uuid4()

        async def inside_the_runtime_loop() -> None:
            # Exactly the production shape: a SYNC call made while the
            # RuntimeLocal loop is running on this thread.
            port._project_evidence(
                correlation_id=correlation_id,
                task_type="document",
                endpoint_ref="local",
                model_id="model-local",
                result=_result(),
                prompt="the prompt",
                source_session_id=None,
                tenant_id="omninode",
                quality_passed=True,
                failure_message="",
                cost_usd=Decimal("0"),
                savings_usd=Decimal("0.5"),
                escalation_count=0,
                attempts=[],
                actual_score=None,
                required_bar=None,
            )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with caplog.at_level(logging.WARNING, logger=_PORT_LOGGER):
                asyncio.run(inside_the_runtime_loop())
            gc.collect()

        connection = sqlite3.connect(db_path)
        try:
            found = connection.execute(
                "SELECT task_type FROM delegation_events WHERE correlation_id = ?",
                (str(correlation_id),),
            ).fetchone()
        finally:
            connection.close()
        assert found == ("document",), "the evidence row was not written"
        assert spy.calls == 0, (
            "the local port reached KafkaSnapshotDeltaPublisher.publish; a tier-0 "
            "local row would be republished to the ambient broker"
        )
        assert _never_awaited(caught) == []
        assert not [
            r
            for r in caplog.records
            if "Failed to project local delegation evidence" in r.getMessage()
        ], "a row that was written must not be reported as a failed projection"


class TestThePublisherRefusesFromARunningLoop:
    """AC2: a typed refusal, raised before any coroutine exists."""

    def test_a_running_loop_is_refused_typed_and_leaks_no_coroutine(self) -> None:
        publisher = KafkaSnapshotDeltaPublisher(bootstrap_servers=_UNROUTABLE_BROKER)

        async def call_from_a_loop() -> None:
            publisher.publish(_message())

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with pytest.raises(SnapshotPublishFromRunningLoopError) as refused:
                asyncio.run(call_from_a_loop())
            gc.collect()

        assert _never_awaited(caught) == []
        assert "worker thread" in str(refused.value)

    def test_positive_control_no_running_loop_still_publishes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The kernel seam's shape: a worker thread with no loop. The guard must
        # not touch it, or every lane-side sync republish would stop.
        sent: list[str] = []

        async def fake_publish(
            self: KafkaSnapshotDeltaPublisher, message: ModelSnapshotDeltaMessage
        ) -> bool:
            sent.append(message.topic)
            return True

        monkeypatch.setattr(KafkaSnapshotDeltaPublisher, "_publish", fake_publish)
        publisher = KafkaSnapshotDeltaPublisher(bootstrap_servers=_UNROUTABLE_BROKER)
        assert publisher.publish(_message()) is True
        assert sent == ["onex.snapshot.projection.delegation.decisions.v1"]


class TestTheLanePathKeepsItsPublisher:
    """The fix is scoped to the local port. A default-built handler is unchanged."""

    def test_a_handler_with_no_injected_publisher_resolves_kafka(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", _UNROUTABLE_BROKER)
        handler = HandlerProjectionDelegation()
        assert isinstance(handler._resolve_publisher(), KafkaSnapshotDeltaPublisher)

    def test_the_error_is_exported_from_the_publisher_module(self) -> None:
        assert (
            _publisher_module.SnapshotPublishFromRunningLoopError
            is SnapshotPublishFromRunningLoopError
        )
