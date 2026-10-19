# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Admission event of a client call (``delegate-skill-admission-decided.v1``).

Published by the orchestrator after the admission decision is committed and
before any dispatch. It is an event topic, so it carries ids, hashes, the
verdict and the effective mode only; the prompt never appears on it.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_delegation_client_mode import EnumDelegationClientMode


class ModelDelegateSkillAdmissionDecided(BaseModel):
    """What the server decided about one client call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    client_call_key: str = Field(min_length=1)
    request_fingerprint: str = Field(min_length=1)
    admitted: bool
    task_type: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    effective_mode: EnumDelegationClientMode


__all__ = ["ModelDelegateSkillAdmissionDecided"]
