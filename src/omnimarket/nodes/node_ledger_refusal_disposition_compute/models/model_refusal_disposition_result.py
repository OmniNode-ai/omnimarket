# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A partial settlement never constitutes admission while any problem remains."""

from pydantic import BaseModel, ConfigDict


class ModelRefusalDispositionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    settled: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
