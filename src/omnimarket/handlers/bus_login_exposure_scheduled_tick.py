# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick of the scheduled job: read the probe's overlay, build the probe, run one probe.

The scheduled job node reaches the bus login exposure probe through this shared module, so no node imports another
node's models package. The probe rules are node_bus_login_exposure_effect's own overlay
(``<exposure_overlay_id>/overlay.yaml`` under ``ONEX_SKILL_OVERLAY_ROOTS``). An unreadable overlay raises ``NodeOverlayError``; the scheduled job node turns it
into a failed run event. The verdict maps the run: WORKING is a completed run, NOT_WORKING or UNKNOWN a failed one.
The detail carries only the verdict, the exposure counts and the read failure value: never a topic name, a group
name or a credential.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import NamedTuple

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_bus_login_exposure_effect.handlers.handler_bus_login_exposure import (
    HandlerBusLoginExposure,
)
from omnimarket.nodes.node_bus_login_exposure_effect.models.model_bus_login_exposure import (
    ModelBusLoginExposureRequest,
)
from omnimarket.nodes.node_bus_login_exposure_effect.overlay import (
    ModelBusLoginExposureOverlay,
)
from omnimarket.nodes.node_bus_login_exposure_effect.protocols.protocol_bus_visibility_source import (
    ProtocolBrokerAdmin,
)

AdminFactory = Callable[[Mapping[str, str]], ProtocolBrokerAdmin]


class ModelBusLoginExposureTickOutcome(NamedTuple):
    """How one scheduled probe went: failed, and the detail the run event carries."""

    failed: bool
    detail: str


def build_exposure_handler(
    overlay: ModelBusLoginExposureOverlay,
    *,
    admin_factory: AdminFactory | None,
    environ: Mapping[str, str] | None,
) -> HandlerBusLoginExposure:
    """The default way to build the probe: its own overlay, the injected admin, the environ."""
    return HandlerBusLoginExposure(
        overlay=overlay, admin_factory=admin_factory, environ=environ
    )


def run_bus_login_exposure_tick(
    *,
    exposure_overlay_id: str,
    environ: Mapping[str, str] | None,
    clock: Callable[[], datetime],
    admin_factory: AdminFactory | None = None,
    build_handler: Callable[..., HandlerBusLoginExposure] | None = None,
) -> ModelBusLoginExposureTickOutcome:
    """Run the bus login exposure probe once, the way the probe's unit does."""
    overlay = load_structured_overlay(
        exposure_overlay_id,
        ModelBusLoginExposureOverlay,
        environ,
    )
    build = build_handler if build_handler is not None else build_exposure_handler
    handler = build(overlay=overlay, admin_factory=admin_factory, environ=environ)
    result = handler.handle(
        ModelBusLoginExposureRequest(correlation_id=uuid.uuid4().hex, now=clock())
    )
    reading = result.reading
    detail = (
        f"{reading.verdict.value} "
        f"exposed_topics={len(reading.exposed_topics)} "
        f"exposed_groups={len(reading.exposed_groups)}"
    )
    if reading.read_failure is not None:
        detail += f" read_failure={reading.read_failure.value}"
    return ModelBusLoginExposureTickOutcome(
        reading.verdict is not EnumReadingVerdict.WORKING, detail
    )


__all__: list[str] = [
    "AdminFactory",
    "ModelBusLoginExposureTickOutcome",
    "build_exposure_handler",
    "run_bus_login_exposure_tick",
]
