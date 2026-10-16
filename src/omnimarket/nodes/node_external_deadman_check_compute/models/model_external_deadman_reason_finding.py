# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One reason the external dead-man raises, with the leg it concerns (OMN-20808)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .enum_external_deadman_reason import EnumExternalDeadmanReason


class ModelExternalDeadmanReasonFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: EnumExternalDeadmanReason
    #: ``primary``, ``deadman`` or ``both``.
    leg: str
    detail: str
