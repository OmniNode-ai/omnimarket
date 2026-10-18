# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One lab daemon whose installed revision is not its source branch tip (OMN-19998).

node_lab_daemon_sync_effect writes these rows to its per-host state file on every host tick, and
node_throughput_tick_decision_compute reads them as its `deploy_drifts` facts. Shared here so the
writer and the reader agree on one shape.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

_TS = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"


class ModelLabDaemonDrift(BaseModel):
    """A daemon on a host that does not run its source branch tip, and since when."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    daemon: str = Field(min_length=1, description="The overlay's daemon name.")
    host: str = Field(min_length=1, description="The lab host the daemon runs on.")
    installed_revision: str | None = Field(
        default=None, description="The installed revision, or None when unreadable."
    )
    source_revision: str | None = Field(
        default=None, description="The source branch tip, or None when unreadable."
    )
    lagging_since: str = Field(
        pattern=_TS,
        description="When the sync first read this lag; unchanged while it persists.",
    )
    branch_until: str | None = Field(
        default=None,
        description="An unexpired branch install's expiry; the lag is held until then.",
    )
    blocker: str | None = Field(
        default=None, description="Why the last install attempt did not converge."
    )
