# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick of the scheduled job: read the probe's overlay, build the probe's ports, run one run.

The scheduled job node reaches the C32 auth-boundary probe through this shared module, so no node imports another
node's models package. The probe rules are the probe's own overlay (``ONEX_C32_PROBE_OVERLAY`` pins the file, else
``<probe_overlay_id>/overlay.yaml`` under ``ONEX_SKILL_OVERLAY_ROOTS``). An unreadable overlay or a result topic the
probe's contract does not declare raises ``ValueError``; the scheduled job node turns it into a failed run event.
The run record goes on the bus: a tick holds no state directory and writes no files.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import NamedTuple

from omnimarket.events.emit_effect_topic_publisher import EmitEffectTopicPublisher
from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_c32_auth_boundary_probe_effect.handlers.handler_c32_auth_boundary_probe import (
    HandlerC32AuthBoundaryProbe,
    build_prober,
    contract_topics,
)
from omnimarket.nodes.node_c32_auth_boundary_probe_effect.models.model_c32_probe import (
    ModelC32ProbeOverlay,
    ModelC32ProbeRequest,
)
from omnimarket.nodes.node_c32_auth_boundary_probe_effect.protocols.protocol_c32_prober import (
    ProtocolC32Prober,
    ProtocolC32ResultPublisher,
)


class ModelC32ProbeTickOutcome(NamedTuple):
    """How one scheduled run went: failed, and the detail the run event carries."""

    failed: bool
    detail: str


def run_c32_probe_tick(
    *,
    probe_overlay_id: str,
    environ: Mapping[str, str] | None,
    clock: Callable[[], datetime],
    publisher: ProtocolC32ResultPublisher | None = None,
    prober: ProtocolC32Prober | None = None,
) -> ModelC32ProbeTickOutcome:
    """Run the C32 auth-boundary probe once, the way the probe node's tick unit does."""
    overlay = load_structured_overlay(
        probe_overlay_id,
        ModelC32ProbeOverlay,
        environ,
        file_env="ONEX_C32_PROBE_OVERLAY",
    )
    if overlay.result_topic not in contract_topics():
        raise ValueError(
            f"result_topic {overlay.result_topic!r} is not a topic the contract declares"
        )
    handler = HandlerC32AuthBoundaryProbe(
        prober=(
            prober
            if prober is not None
            else build_prober(overlay, overlay.default_profile)
        ),
        publisher=(publisher if publisher is not None else EmitEffectTopicPublisher()),
        topic=overlay.result_topic,
        profile=overlay.default_profile,
        clock=clock,
    )
    result = handler.handle(
        ModelC32ProbeRequest(
            correlation_id=f"c32-probe-{uuid.uuid4().hex[:12]}",
            trigger="schedule",
        )
    )
    if not result.emitted:
        return ModelC32ProbeTickOutcome(
            True, "the probe record was not accepted by the bus"
        )
    ok = sum(1 for clause in result.clauses if clause.ok)
    detail = (
        f"verdict={result.verdict.value} clauses_ok={ok}/{len(result.clauses)} "
        f"profile={result.profile}"
    )
    return ModelC32ProbeTickOutcome(result.conclusion != "success", detail)


__all__: list[str] = [
    "ModelC32ProbeTickOutcome",
    "run_c32_probe_tick",
]
