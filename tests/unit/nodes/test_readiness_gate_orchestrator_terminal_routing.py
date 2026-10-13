# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A blocked gate decision is published on the blocked terminal topic.

The readiness gate declares two terminal events: ``readiness-gate-completed``
(success) and ``readiness-gate-blocked`` (failure). The handler returns one
``ModelDeploymentReadinessResult`` either way, and that model carries no
verdict field the runtime reads (``readiness_state`` is not one of them), so a
BLOCKED decision was published on the success topic and the blocked topic never
carried an event.

These tests drive the entry point the runtime uses: the dispatch adapter around
the real handler, then the production result applier built from the real
contract file. Each case asserts which topic the one returned result lands on.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
import yaml
from omnibase_compat.contracts.evidence_pipeline.wire.model_deployment_readiness_result import (
    ModelDeploymentReadinessResult,
)
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.protocols import ProtocolEventBusLike
from omnibase_infra.runtime.auto_wiring.handler_wiring import _make_dispatch_callback
from omnibase_infra.runtime.service_dispatch_result_applier import (
    build_contract_result_applier,
)

from omnimarket.nodes.node_readiness_gate_orchestrator.handlers.handler_readiness_gate_orchestrator import (
    HandlerReadinessGateOrchestrator,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_readiness_gate_orchestrator"
)
CONTRACT_PATH = NODE_DIR / "contract.yaml"


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


class _CapturingBus:
    """The runtime's publish boundary: records (topic, envelope) per publish."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ModelEventEnvelope[Any]]] = []

    async def publish_envelope(
        self, envelope: object, topic: str, *, key: bytes | None = None
    ) -> None:
        assert isinstance(envelope, ModelEventEnvelope)
        self.calls.append((topic, envelope))


def _gap_report(**overrides: object) -> dict[str, object]:
    correlation_id = str(uuid4())
    report: dict[str, object] = {
        "correlation_id": correlation_id,
        "validation_run_id": str(uuid4()),
        "deployment_id": f"synthetic-gate-{correlation_id[:8]}",
        "generated_at": "2026-10-12T12:00:00+00:00",
        "validator_version": "synthetic-1",
    }
    report.update(overrides)
    return report


async def _published(payload: dict[str, object]) -> list[tuple[str, Any]]:
    """Dispatch one consumed record the way the runtime does; return the publishes."""
    contract = _contract()
    callback = _make_dispatch_callback(HandlerReadinessGateOrchestrator(), None)
    envelope = ModelEventEnvelope[object](
        payload=payload,
        correlation_id=uuid4(),
        source_tool="onex.run-node",
        target_tool="node_readiness_gate_orchestrator",
    )
    result = await callback(envelope)
    assert result is not None
    bus = _CapturingBus()
    await build_contract_result_applier(
        event_bus=cast("ProtocolEventBusLike", bus),
        contract_path=CONTRACT_PATH,
        publish_topics=contract["event_bus"]["publish_topics"],
        terminal_event=contract["terminal_event"],
    ).apply(result)
    return bus.calls


COMPLETED = "onex.evt.omnimarket.readiness-gate-completed.v1"
BLOCKED = "onex.evt.omnimarket.readiness-gate-blocked.v1"


def test_blocking_gap_report_is_published_on_the_blocked_topic() -> None:
    gap = _gap_report(
        gap_classifications={"synthetic-ref-1": "MISSING"},
        missing_evidence_refs=["synthetic-ref-1"],
    )

    calls = asyncio.run(_published(gap))

    assert [topic for topic, _ in calls] == [BLOCKED]
    published = calls[0][1].payload
    dumped = published.model_dump() if hasattr(published, "model_dump") else published
    assert dumped["readiness_state"] == "BLOCKED"
    assert dumped["correlation_id"] == gap["correlation_id"]
    assert dumped["blocking_reason_codes"] == ("synthetic-ref-1",) or dumped[
        "blocking_reason_codes"
    ] == ["synthetic-ref-1"]


def test_blocked_payload_is_still_a_deployment_readiness_result() -> None:
    gap = _gap_report(gap_classifications={"synthetic-ref-1": "HASH_MISMATCH"})

    calls = asyncio.run(_published(gap))

    published = calls[0][1].payload
    dumped = published.model_dump() if hasattr(published, "model_dump") else published
    assert ModelDeploymentReadinessResult.model_validate(dumped).readiness_state == (
        "BLOCKED"
    )


def test_clean_gap_report_is_published_on_the_completed_topic() -> None:
    calls = asyncio.run(_published(_gap_report()))

    assert [topic for topic, _ in calls] == [COMPLETED]
    published = calls[0][1].payload
    dumped = published.model_dump() if hasattr(published, "model_dump") else published
    assert dumped["readiness_state"] == "READY"


def test_completed_topic_is_declared_for_the_plain_result_class() -> None:
    """The completed topic is mapped by class, not reached through terminal_event.

    A non-blocked decision returns the plain ``ModelDeploymentReadinessResult``;
    the contract names that class for the completed topic and the blocked class
    for the blocked topic, so neither result falls through to ``terminal_event``.
    """
    routes = {
        entry["event_type"]: entry["topic"]
        for entry in _contract().get("published_events", [])
    }

    assert routes.get("DeploymentReadinessResult") == COMPLETED
    assert routes.get("ReadinessGateBlocked") == BLOCKED


def test_degraded_gap_report_is_published_on_the_completed_topic() -> None:
    gap = _gap_report(gap_classifications={"synthetic-ref-1": "STALE"})

    calls = asyncio.run(_published(gap))

    assert [topic for topic, _ in calls] == [COMPLETED]
    published = calls[0][1].payload
    dumped = published.model_dump() if hasattr(published, "model_dump") else published
    assert dumped["readiness_state"] == "DEGRADED"


def test_prescored_blocked_readiness_is_published_on_the_blocked_topic() -> None:
    """The readiness-scored branch (readiness_state in the payload) routes the same way."""
    scored = ModelDeploymentReadinessResult(
        correlation_id=str(uuid4()),
        validation_run_id=str(uuid4()),
        deployment_id="synthetic-gate-prescored",
        readiness_state="BLOCKED",
        scored_at="2026-10-12T12:00:00+00:00",
        validator_version="synthetic-1",
        gap_report_hash="sha256:" + "0" * 64,
        topology_affecting=False,
        blocking_reason_codes=("synthetic-ref-1",),
    )

    calls = asyncio.run(_published(scored.model_dump(mode="json")))

    assert [topic for topic, _ in calls] == [BLOCKED]
