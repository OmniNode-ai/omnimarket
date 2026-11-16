# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for the live-grep probe check."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

PROBE_TERM_DELIMITER = "[.]"


class ModelLiveProbeRequest(BaseModel):
    """The input of the live-grep probe check: the probe's live-grep text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe: str


class ModelLiveProbeResult(BaseModel):
    """The result of the live-grep probe check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base: bool | None
    contract_local_nodes: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
