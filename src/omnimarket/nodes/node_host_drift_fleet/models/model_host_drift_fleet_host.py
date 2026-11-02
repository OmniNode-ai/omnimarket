# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Latest heartbeat and deduplicated resolution history for one host."""

from pydantic import AwareDatetime

from ._base import ModelFrozen
from .model_host_drift_open_item import ModelHostDriftOpenItem
from .model_host_drift_resolved_event import ModelHostDriftResolvedEvent


class ModelHostDriftFleetHost(ModelFrozen):
    host: str
    last_heartbeat_at: AwareDatetime | None = None
    last_check_id: str | None = None
    open: tuple[ModelHostDriftOpenItem, ...] = ()
    resolved_count: int = 0
    last_resolved: tuple[ModelHostDriftResolvedEvent, ...] = ()
    resolved_ids: tuple[str, ...] = ()
