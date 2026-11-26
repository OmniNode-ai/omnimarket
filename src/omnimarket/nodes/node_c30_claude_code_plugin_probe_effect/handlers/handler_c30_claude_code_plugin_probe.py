# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the C30 Claude Code plugin probe once and put the run on the bus (OMN-19966, MVP launch plan task M17).

``handle(request) -> result`` runs the injected prober, builds the run record (a ``ModelMvpRun``-shaped record that the
launch reading M1 reads for C30) and publishes it on the overlay's result topic. A prober that raises is an ERROR run
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
from omnimarket.nodes.node_c30_claude_code_plugin_probe_effect.handlers.adapter_c30_probers import (
    InMemoryC30Prober,
    InMemoryC30Publisher,
    LocalOnexC30Prober,
)
from omnimarket.nodes.node_c30_claude_code_plugin_probe_effect.models.model_c30_probe import (
    EnumC30Verdict,
    ModelC30ProbeOverlay,
    ModelC30ProbeReading,
    ModelC30ProbeRequest,
    ModelC30ProbeResult,
)
from omnimarket.nodes.node_c30_claude_code_plugin_probe_effect.protocols.protocol_c30_prober import (
    ProtocolC30Prober,
    ProtocolC30ResultPublisher,
)

NODE = "node_c30_claude_code_plugin_probe_effect"
OVERLAY_ID = NODE
OVERLAY_FILE_ENV = "ONEX_C30_PROBE_OVERLAY"
EVENT_TYPE = "c30.claude_code_plugin_probe.completed"
CRITERION = "C30"


def contract_topics() -> tuple[str, ...]:
    contract = yaml.safe_load(
        (Path(__file__).parent.parent / "contract.yaml").read_text(encoding="utf-8")
    )
    return tuple(str(t) for t in contract["event_bus"]["publish_topics"])


def load_overlay(path: Path | None = None) -> ModelC30ProbeOverlay:
    explicit = yaml.safe_load(path.read_text(encoding="utf-8")) if path else None
    overlay = load_structured_overlay(
        OVERLAY_ID,
        ModelC30ProbeOverlay,
        explicit=explicit,
        file_env=OVERLAY_FILE_ENV,
    )
    if overlay.result_topic not in contract_topics():
        raise ValueError(
            f"result_topic {overlay.result_topic!r} is not a topic the contract declares"
        )
    return overlay


def build_prober(
    overlay: ModelC30ProbeOverlay, profile: str | None
) -> ProtocolC30Prober:
    chosen = overlay.profile(profile)
    if chosen.kind == "tick":
        return LocalOnexC30Prober(
            str(Path(sys.executable).parent), graded_packages=chosen.graded_packages
        )
    if chosen.kind == "local":
        assert chosen.onex_bin_dir is not None
        return LocalOnexC30Prober(
            chosen.onex_bin_dir, graded_packages=chosen.graded_packages
        )
    return InMemoryC30Prober()


def _new_run_id(now: datetime) -> str:
    return f"c30-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


class HandlerC30ClaudeCodePluginProbe:
    def __init__(
        self,
        *,
        prober: ProtocolC30Prober,
        publisher: ProtocolC30ResultPublisher | None = None,
        topic: str,
        profile: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        run_id_factory: Callable[[datetime], str] = _new_run_id,
    ) -> None:
        self._prober = prober
        self._publisher = publisher or InMemoryC30Publisher()
        self._topic = topic
        self._profile = profile
        self._clock = clock
        self._run_id = run_id_factory

    def handle(self, request: ModelC30ProbeRequest) -> ModelC30ProbeResult:
        created = self._clock()
        error: str | None = None
        try:
            reading = self._prober.run_probe()
            verdict = reading.verdict()
        except Exception as exc:  # the run is recorded as an ERROR, never lost
            reading = ModelC30ProbeReading()
            verdict = EnumC30Verdict.ERROR
            error = f"{type(exc).__name__}: {exc}"[-2000:]
        if verdict is EnumC30Verdict.ERROR and error is None:
            error = "preconditions held and no clause was graded"
        result = ModelC30ProbeResult(
            correlation_id=request.correlation_id,
            run_id=self._run_id(created),
            created_at=created,
            finished_at=self._clock(),
            event=request.trigger,
            status="errored" if verdict is EnumC30Verdict.ERROR else "completed",
            conclusion="success" if verdict is EnumC30Verdict.PASS else "failure",
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
