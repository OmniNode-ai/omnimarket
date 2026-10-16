# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One watchdog leg's mirror variable: bus facts, written every 5 minutes (OMN-20808)."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from .model_external_deadman_backlog import ModelExternalDeadmanBacklog
from .model_external_deadman_canary import ModelExternalDeadmanCanary

MIRROR_SCHEMA_VERSION = "external-deadman-mirror/v1"


class ModelExternalDeadmanMirror(BaseModel):
    """What a leg holds on the bus that an observer outside the lab cannot read.

    The mirror is not a transport between services: the legs talk only on the bus. ``canary`` is
    ``None`` while a leg has no canary state to report, which makes no claim either way.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["external-deadman-mirror/v1"]
    leg: Literal["primary", "deadman"]
    written_at: AwareDatetime
    own_heartbeat_at: AwareDatetime | None = None
    peer_heartbeat_at: AwareDatetime | None = None
    undelivered: ModelExternalDeadmanBacklog
    unrecorded: ModelExternalDeadmanBacklog
    canary: ModelExternalDeadmanCanary | None = None
