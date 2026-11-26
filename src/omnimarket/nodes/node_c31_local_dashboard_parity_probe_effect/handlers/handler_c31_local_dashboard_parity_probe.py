# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the C31 local dashboard parity probe once and put the run on the bus (OMN-19966, MVP launch plan task M17).

``handle(request) -> result`` runs the injected prober, builds the run record (a ``ModelMvpRun``-shaped record that the
launch reading M1 reads for C31) and publishes it on the overlay's result topic. A prober that raises is an ERROR run
that concludes ``failure`` and is still emitted: a broken probe must read red, never silent.
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_c31_local_dashboard_parity_probe_effect.handlers.adapter_c31_probers import (
    InMemoryC31Prober,
    InMemoryC31Publisher,
    LocalOnexC31Prober,
)
from omnimarket.nodes.node_c31_local_dashboard_parity_probe_effect.models.model_c31_probe import (
    EnumC31Verdict,
    ModelC31ProbeOverlay,
    ModelC31ProbeReading,
    ModelC31ProbeRequest,
    ModelC31ProbeResult,
)
from omnimarket.nodes.node_c31_local_dashboard_parity_probe_effect.protocols.protocol_c31_prober import (
    ProtocolC31Prober,
    ProtocolC31ResultPublisher,
)

NODE = "node_c31_local_dashboard_parity_probe_effect"
OVERLAY_ID = NODE
OVERLAY_FILE_ENV = "ONEX_C31_PROBE_OVERLAY"
EVENT_TYPE = "c31.local_dashboard_parity_probe.completed"
CRITERION = "C31"


def contract_topics() -> tuple[str, ...]:
    contract = yaml.safe_load(
        (Path(__file__).parent.parent / "contract.yaml").read_text(encoding="utf-8")
    )
    return tuple(str(t) for t in contract["event_bus"]["publish_topics"])


def load_overlay(path: Path | None = None) -> ModelC31ProbeOverlay:
    explicit = yaml.safe_load(path.read_text(encoding="utf-8")) if path else None
    overlay = load_structured_overlay(
        OVERLAY_ID,
        ModelC31ProbeOverlay,
        explicit=explicit,
        file_env=OVERLAY_FILE_ENV,
    )
    if overlay.result_topic not in contract_topics():
        raise ValueError(
            f"result_topic {overlay.result_topic!r} is not a topic the contract declares"
        )
    return overlay


def build_prober(
    overlay: ModelC31ProbeOverlay, profile: str | None
) -> ProtocolC31Prober:
    chosen = overlay.profile(profile)
    if chosen.kind == "tick":
        return LocalOnexC31Prober(
            str(Path(sys.executable).parent), graded_packages=chosen.graded_packages
        )
    if chosen.kind == "local":
        assert chosen.onex_bin_dir is not None
        return LocalOnexC31Prober(
            chosen.onex_bin_dir, graded_packages=chosen.graded_packages
        )
    return InMemoryC31Prober()


def _new_run_id(now: datetime) -> str:
    return f"c31-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


class HandlerC31LocalDashboardParityProbe:
    def __init__(
        self,
        *,
        prober: ProtocolC31Prober,
        publisher: ProtocolC31ResultPublisher | None = None,
        topic: str,
        profile: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        run_id_factory: Callable[[datetime], str] = _new_run_id,
    ) -> None:
        self._prober = prober
        self._publisher = publisher or InMemoryC31Publisher()
        self._topic = topic
        self._profile = profile
        self._clock = clock
        self._run_id = run_id_factory

    def handle(self, request: ModelC31ProbeRequest) -> ModelC31ProbeResult:
        created = self._clock()
        error: str | None = None
        try:
            reading = self._prober.run_probe()
            verdict = reading.verdict()
        except Exception as exc:  # the run is recorded as an ERROR, never lost
            reading = ModelC31ProbeReading()
            verdict = EnumC31Verdict.ERROR
            error = f"{type(exc).__name__}: {exc}"[-2000:]
        if verdict is EnumC31Verdict.ERROR and error is None:
            error = "preconditions held and no clause was graded"
        result = ModelC31ProbeResult(
            correlation_id=request.correlation_id,
            run_id=self._run_id(created),
            created_at=created,
            finished_at=self._clock(),
            event=request.trigger,
            status="errored" if verdict is EnumC31Verdict.ERROR else "completed",
            conclusion="success" if verdict is EnumC31Verdict.PASS else "failure",
            verdict=verdict,
            profile=self._profile,
            clauses=reading.clauses,
            preconditions=reading.preconditions,
            versions=reading.versions,
            error=error,
        )
        accepted = self._publisher.publish(
            topic=self._topic,
            event_type=EVENT_TYPE,
            payload={
                **result.model_dump(mode="json", exclude={"emitted"}),
                "criterion": CRITERION,
                "mvp_run": result.mvp_run(),
            },
            event_id=result.run_id,
            correlation_id=request.correlation_id,
            partition_key=CRITERION,
        )
        return result.model_copy(update={"emitted": accepted})
