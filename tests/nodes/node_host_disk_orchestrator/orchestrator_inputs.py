# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders shared by the host disk orchestrator and chain tests (OMN-23866). Synthetic hosts and values only."""

from __future__ import annotations

import copy
import datetime as dt
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.models.model_host_disk_decision import ModelHostDiskPolicy
from omnimarket.models.model_host_disk_orchestrator import (
    ModelHostDiskEvictCompleted,
    ModelHostDiskEvictFailed,
    ModelHostDiskEvictRequested,
    ModelHostDiskOrchestratorOverlay,
)
from omnimarket.models.model_host_disk_reading import (
    ModelDiskClassReading,
    ModelDiskGroup,
    ModelHostDiskRead,
    ModelHostDiskVolume,
)
from omnimarket.nodes.node_host_disk_orchestrator.handlers.handler_host_disk_low_reported import (
    HandlerHostDiskLowReported,
)
from omnimarket.nodes.node_host_disk_orchestrator.handlers.handler_host_disk_results import (
    HandlerHostDiskEvictCompleted,
    HandlerHostDiskEvictFailed,
)
from omnimarket.nodes.node_host_disk_orchestrator.handlers.handler_host_disk_scheduled import (
    NODE_NAME,
    HandlerHostDiskScheduled,
)
from omnimarket.nodes.node_host_disk_orchestrator.handlers.run_state import (
    HostDiskOrchestratorState,
    InMemoryHostDiskRows,
)
from tests.nodes.node_host_disk_decision_compute.decision_inputs import (
    DAY,
    GIB,
    OVERLAY_EXAMPLE,
)

T0 = dt.datetime(2026, 11, 14, 4, 0, tzinfo=dt.UTC)
NODE_DIR = (
    Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes" / NODE_NAME
)
HOSTS = ("example-host-a", "example-host-b")


def _contract_topic(fragment: str) -> str:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    topics = (
        contract["event_bus"]["subscribe_topics"]
        + contract["event_bus"]["publish_topics"]
    )
    found = [t for t in topics if fragment in t]
    assert len(found) == 1, (fragment, found)
    return str(found[0])


READ_TOPIC = _contract_topic(".host-disk-read.")
REQUESTED_TOPIC = _contract_topic(".host-disk-evict-requested.")
COMPLETED_TOPIC = _contract_topic(".host-disk-evict-completed.")
FAILED_TOPIC = _contract_topic(".host-disk-evict-failed.")
SLACK_TOPIC = _contract_topic(".slack-publish.")
RUN_TOPIC = _contract_topic(".host-disk-run-completed.")
LOW_TOPIC = _contract_topic(".host-disk-low-reported.")


def overlay() -> ModelHostDiskOrchestratorOverlay:
    data = yaml.safe_load((NODE_DIR / "overlays" / "in_memory.yaml").read_text())
    return ModelHostDiskOrchestratorOverlay.model_validate(data)


def policy(*, floor_free_bytes: int | None = None) -> ModelHostDiskPolicy:
    """The example policy; ``floor_free_bytes`` replaces the default floor (a test overlay)."""
    document: dict[str, Any] = copy.deepcopy(
        yaml.safe_load(OVERLAY_EXAMPLE.read_text())
    )
    if floor_free_bytes is not None:
        document["defaults"]["floor_free_bytes"] = floor_free_bytes
        document["hosts"]["example-host-b"].pop("floor_free_bytes", None)
    return ModelHostDiskPolicy.model_validate(document)


def read_event(
    host: str,
    *,
    read_at: dt.datetime = T0,
    free_bytes: int = 500 * GIB,
    stale_revisions: int = 2,
) -> ModelHostDiskRead:
    """A host reading with a uv cache holding ``stale_revisions`` revisions past every keep window."""
    groups = [
        ModelDiskGroup(
            package="pkg",
            revision=f"old{i}",
            copies=1,
            bytes=GIB,
            newest_use_age_seconds=(60 + i) * DAY,
            oldest_use_age_seconds=(60 + i) * DAY,
            held_copies=0,
        )
        for i in range(stale_revisions)
    ]
    groups.append(
        ModelDiskGroup(
            package="pkg",
            revision="new",
            copies=1,
            bytes=GIB,
            newest_use_age_seconds=DAY // 2,
            oldest_use_age_seconds=DAY // 2,
            held_copies=0,
        )
    )
    total = 1000 * GIB
    return ModelHostDiskRead(
        correlation_id=f"read-{host}-{read_at.isoformat()}",
        host_name=host,
        read_at=read_at,
        volumes=(
            ModelHostDiskVolume(
                path="/",
                total_bytes=total,
                free_bytes=free_bytes,
                used_bytes=total - free_bytes,
            ),
        ),
        classes=(
            ModelDiskClassReading(
                class_name="uv_cache",
                status="read",
                total_bytes=len(groups) * GIB,
                entry_count=len(groups),
                revision_count=len(groups),
                groups=tuple(groups),
            ),
        ),
    )


def tick(now: dt.datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=now,
        tick_id=uuid.uuid4(),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=uuid.uuid4(),
        scheduler_id="omn23866-test",
        tick_interval_ms=1000,
    )


class Orchestrator:
    """The orchestrator's handlers built the way the runtime builds them: real gate, real state, real compute."""

    def __init__(
        self,
        *,
        pol: ModelHostDiskPolicy | None = None,
        ovl: ModelHostDiskOrchestratorOverlay | None = None,
        clock: Callable[[], dt.datetime] = lambda: T0,
    ) -> None:
        self.overlay = ovl or overlay()
        self.policy = pol or policy()
        self.state = HostDiskOrchestratorState(rows=InMemoryHostDiskRows())
        load = lambda: self.policy  # noqa: E731
        self.scheduled = HandlerHostDiskScheduled(
            gate=ScheduleGate(NODE_NAME, declaration=self.overlay),
            state=self.state,
            policy_loader=load,
            clock=clock,
        )
        self.low = HandlerHostDiskLowReported(
            overlay=self.overlay, state=self.state, policy_loader=load
        )
        self.completed = HandlerHostDiskEvictCompleted(self.state, clock=clock)
        self.failed = HandlerHostDiskEvictFailed(self.state, clock=clock)

    def read(self, event: ModelHostDiskRead) -> None:
        assert isinstance(self.state.rows, InMemoryHostDiskRows)
        self.state.rows.record_read(event)


def payloads(output: ModelHandlerOutput[None]) -> list[Any]:
    return [e.payload for e in output.events]


def completed_for(request: ModelHostDiskEvictRequested) -> ModelHostDiskEvictCompleted:
    return ModelHostDiskEvictCompleted(
        idempotency_key=request.idempotency_key,
        host_name=request.command.host_name,
        class_name=request.command.class_name,
        practice_mode=request.practice_mode,
        evicted_count=len(request.command.targets),
        bytes_freed=sum(t.bytes for t in request.command.targets),
    )


def failed_for(
    request: ModelHostDiskEvictRequested, reason: Any, detail: str = "refused"
) -> ModelHostDiskEvictFailed:
    return ModelHostDiskEvictFailed(
        idempotency_key=request.idempotency_key,
        host_name=request.command.host_name,
        class_name=request.command.class_name,
        reason=reason,
        detail=detail,
    )
