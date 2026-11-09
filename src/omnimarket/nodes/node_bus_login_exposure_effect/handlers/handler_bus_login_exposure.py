# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerBusLoginExposure: read what one broker login can see and report every extra grant.

EFFECT handler: reads only. The source (in-memory fixture or Redpanda admin reads as the login) and
every setting come from the overlay. A topic or group the login sees that the overlay does not
allow is an exposure (NOT_WORKING). A view that could not be read, or is older than the overlay's
max age, reads UNKNOWN and the run ends in the typed failure event (``event_for``); it never
reads as an empty, clean view.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_bus_login_exposure_effect.adapters.inmemory_bus_source import (
    InMemoryBusSource,
)
from omnimarket.nodes.node_bus_login_exposure_effect.adapters.redpanda_bus_source import (
    AdminFactory,
    RedpandaBusSource,
)
from omnimarket.nodes.node_bus_login_exposure_effect.models.model_bus_login_exposure import (
    ModelBusLoginExposureReading,
    ModelBusLoginExposureRequest,
    ModelBusLoginExposureResult,
    ModelBusVisibilityRead,
)
from omnimarket.nodes.node_bus_login_exposure_effect.overlay import (
    ModelBusLoginExposureOverlay,
    ModelExposureSettings,
    ModelInMemorySource,
    load_overlay,
)
from omnimarket.nodes.node_bus_login_exposure_effect.protocols.protocol_bus_visibility_source import (
    ProtocolBusVisibilitySource,
)

_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"


def load_topics() -> dict[str, str]:
    """The topics by role, read from the contract (the handler holds no topic string)."""
    loaded = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return dict(loaded["topics"])


def _exposed(
    visible: tuple[str, ...], allowed: tuple[str, ...], prefixes: tuple[str, ...]
) -> tuple[str, ...]:
    allowed_set = set(allowed)
    return tuple(
        sorted(
            name
            for name in set(visible)
            if name not in allowed_set and not name.startswith(prefixes)
        )
    )


def judge(
    read: ModelBusVisibilityRead,
    settings: ModelExposureSettings,
    request: ModelBusLoginExposureRequest,
) -> ModelBusLoginExposureReading:
    """Pure reading of one view against the allowed lists."""
    stale = request.now - read.read_at > timedelta(seconds=settings.max_age_seconds)
    topics = tuple(sorted(set(read.topics)))
    groups = tuple(sorted(set(read.groups)))
    exposed_topics = _exposed(
        topics, settings.allowed_topics, tuple(settings.allowed_topic_prefixes)
    )
    exposed_groups = _exposed(
        groups, settings.allowed_groups, tuple(settings.allowed_group_prefixes)
    )
    if read.read_failure is not None or stale:
        verdict = EnumReadingVerdict.UNKNOWN
    elif exposed_topics or exposed_groups:
        verdict = EnumReadingVerdict.NOT_WORKING
    else:
        verdict = EnumReadingVerdict.WORKING
    return ModelBusLoginExposureReading(
        verdict=verdict,
        visible_topics=topics,
        visible_groups=groups,
        exposed_topics=exposed_topics,
        exposed_groups=exposed_groups,
        read_at=read.read_at,
        stale=stale,
        read_failure=read.read_failure,
    )


class HandlerBusLoginExposure:
    def __init__(
        self,
        overlay: ModelBusLoginExposureOverlay | None = None,
        admin_factory: AdminFactory | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._overlay = overlay
        self._admin_factory = admin_factory
        self._environ = environ

    def _source(
        self, overlay: ModelBusLoginExposureOverlay
    ) -> ProtocolBusVisibilitySource:
        source = overlay.source
        if isinstance(source, ModelInMemorySource):
            return InMemoryBusSource(source.fixture)
        return RedpandaBusSource(
            source,
            os.environ if self._environ is None else self._environ,
            self._admin_factory,
        )

    def handle(
        self, request: ModelBusLoginExposureRequest
    ) -> ModelBusLoginExposureResult:
        overlay = self._overlay or load_overlay(self._environ)
        read = self._source(overlay).read(now=request.now)
        return ModelBusLoginExposureResult(
            correlation_id=request.correlation_id,
            backend=overlay.source.backend,
            reading=judge(read, overlay.settings, request),
        )

    @staticmethod
    def event_for(result: ModelBusLoginExposureResult) -> tuple[str, dict[str, Any]]:
        """The terminal event of a run: the failure topic when the view was not read."""
        topics = load_topics()
        reading = result.reading
        topic = (
            topics["probe_failed"]
            if reading.read_failure is not None or reading.stale
            else topics["probe_completed"]
        )
        return topic, result.model_dump(mode="json")


__all__ = ["HandlerBusLoginExposure", "judge", "load_topics"]
