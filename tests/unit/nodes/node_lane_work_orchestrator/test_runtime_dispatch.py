# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlapping inputs fold one at a time under the runtime's own dispatcher (OMN-26721 AC2).

The real contract is wired by ``wire_from_manifest`` on an in-memory bus and the runtime's
dispatch engine. A worker event, an effect completion, a PR observation and a tick are
published at once; the fold must give each its own receipt with consecutive seq values and
lose none. Intake handlers re-publish typed inputs onto the ingress topic; only the fold
handler folds.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import timedelta
from uuid import uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import BaseModel

from omnimarket.events.pr_state import EnumPrState, ModelPrStateObservedEvent
from omnimarket.nodes.node_lab_work_unit_effect.models.model_lab_work_unit import (
    EnumLabWorkUnitStatus,
    ModelLabWorkUnitReceipt,
)
from omnimarket.nodes.node_lane_work_orchestrator.event_topics import (
    CONTRACT_PATH,
    ROLE_INGRESS,
    ROLE_TRANSITION,
    topic_for,
)
from omnimarket.nodes.node_lane_work_orchestrator.process_state import (
    install_orchestrator,
    reset_orchestrator,
)

from .rig_lane_work import HEAD, T0, Rig

pytestmark = pytest.mark.unit

LAB_COMPLETED = "onex.evt.omnimarket.lab-work-unit-completed.v1"
PR_OBSERVED = "onex.evt.omnimarket.pr-state-observed.v1"
TICK = "onex.intent.platform.runtime-tick.v1"


@pytest.fixture
def installed() -> Iterator[Rig]:
    rig = Rig()
    rig.to_proving()  # the proof is outstanding at HEAD
    install_orchestrator(rig.orchestrator)
    yield rig
    reset_orchestrator()


def _wire(model: BaseModel) -> bytes:
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=model.model_dump(mode="json"), correlation_id=uuid4()
    )
    return envelope.model_dump_json().encode("utf-8")


def _observation() -> ModelPrStateObservedEvent:
    at = (T0 + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ModelPrStateObservedEvent(
        repo="OmniNode-ai/omnimarket",
        pr_number=7,
        state=EnumPrState.OPEN,
        head_sha=HEAD,
        base="dev",
        head_ref="b",
        title="t",
        draft=True,
        author="a",
        author_is_bot=False,
        labels=(),
        armed=False,
        queued=False,
        watcher_class="green",
        ci_verdict="GREEN",
        red_contexts=(),
        pending_contexts=(),
        ci_read_at=at,
        merged_at="",
        observed_at=at,
    )


async def _publish_at_once(rig: Rig) -> list[dict[str, object]]:
    manifest = discover_contracts_from_paths([CONTRACT_PATH])
    assert not manifest.errors, manifest.errors
    bus = EventBusInmemory(environment="lane-work-boot", group="lane-work-boot")
    await bus.start()
    seen: list[dict[str, object]] = []

    async def _collect(message: object) -> None:
        seen.append(json.loads(bytes(getattr(message, "value", b""))))

    await bus.subscribe(
        topic_for(ROLE_TRANSITION), on_message=_collect, group_id="probe-transition"
    )
    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
        engine,
        event_bus=bus,
        environment="local",
    )
    assert report.total_failed == 0, report
    engine.freeze()

    proof = rig.outstanding("lab_proof")
    assert proof is not None
    worker = rig.ingress("fix_committed")  # a worker input the loop has no edge for
    effect = ModelLabWorkUnitReceipt(
        work_unit_id=proof,
        target_host="lab-pool",
        host="lab-pool",
        repo="OmniNode-ai/omnimarket",
        commit_sha=HEAD,
        status=EnumLabWorkUnitStatus.COMPLETED,
        exit_code=0,
    )
    tick = ModelRuntimeTick(
        now=T0 + timedelta(days=1),
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=T0 + timedelta(days=1),
        correlation_id=uuid4(),
        scheduler_id="test",
        tick_interval_ms=1000,
    )
    await asyncio.gather(
        bus.publish(topic_for(ROLE_INGRESS), None, _wire(worker), None),
        bus.publish(LAB_COMPLETED, None, _wire(effect), None),
        bus.publish(PR_OBSERVED, None, _wire(_observation()), None),
        bus.publish(TICK, None, _wire(tick), None),
    )
    for _ in range(100):  # let the hops (intake, then ingress) settle
        await asyncio.sleep(0.1)
        if len(rig.orchestrator.journal.read(ROLE_TRANSITION)) >= len(rig.sent) + 4:
            break
    await asyncio.sleep(0.5)
    await bus.close()
    return seen


async def test_overlapping_inputs_fold_one_at_a_time_with_consecutive_seq(
    installed: Rig,
) -> None:
    rig = installed
    before = len(rig.orchestrator.journal.read(ROLE_TRANSITION))
    await _publish_at_once(rig)
    records = [payload for _, payload in rig.orchestrator.journal.read(ROLE_TRANSITION)]
    new = records[before:]
    seqs = [record["receipt"]["seq"] for record in new]
    assert len(new) >= 4, (
        f"an input was lost: {[r['receipt']['event_kind'] for r in new]}"
    )
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), seqs
    assert seqs[0] == before + 1
    kinds = {record["receipt"]["event_kind"] for record in new}
    assert "fix_committed" in kinds  # the worker's input
    assert "proof_passed" in kinds  # the effect completion
    assert "deadline_passed" in kinds  # the tick
    ids = [record["receipt"]["input_id"] for record in new]
    assert len(ids) == len(set(ids)), "an input folded twice"
