# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for the pool-run verb: the pool driver's own phase text, read by the node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelPoolRunRequest(BaseModel):
    """What the pool driver hands over: the clone phase's text and the probe phase's text, unread."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    clone: str
    probe: str
