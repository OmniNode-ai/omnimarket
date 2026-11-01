# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Heartbeat payload; newer producer extensions are ignored on decode."""

from pydantic import AwareDatetime, ConfigDict

from ._base import ModelFrozen
from .model_host_drift_consumer_health import ModelHostDriftConsumerHealth
from .model_host_drift_open_item import ModelHostDriftOpenItem


class ModelHostDriftHeartbeatEvent(ModelFrozen):
    model_config = ConfigDict(frozen=True, extra="ignore")
    host: str
    check_id: str
    checked_at: AwareDatetime
    open: tuple[ModelHostDriftOpenItem, ...]
    role: str = ""
    interval_seconds: int = 0
    items_total: int = 0
    items_ok: int = 0
    items_drift: int = 0
    items_failed: int = 0
    previous_check_at: AwareDatetime | None = None
    gap_seconds: int | None = None
    manifest_schema: str = "host-drift-manifest.v1"
    generation: int | None = None
    consumer: ModelHostDriftConsumerHealth | None = None
