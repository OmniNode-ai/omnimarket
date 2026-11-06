# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The boundary that runs one probe attempt as the principal."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from ..models.model_read_only_principal import ModelProbeAttempt


class ModelAttemptRun(BaseModel):
    """What an attempt did. ``text`` is read for refusal markers and never reported."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int
    text: str = ""


class ProtocolAttemptRunner(Protocol):
    """Run one attempt with only the environment variables it names."""

    def run(self, attempt: ModelProbeAttempt) -> ModelAttemptRun: ...
