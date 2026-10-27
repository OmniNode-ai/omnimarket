# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Consumer-side declaration of the already-tolerated client terminal echoes."""

from pydantic import Field, StrictBool

from omnimarket.models.claude_code_binding import EnumClientMaxMode
from omnimarket.models.delegation.wire.model_delegate_skill_admission_decided import (
    ModelDelegateSkillAdmissionDecided,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
)


class ModelClientAdmission(ModelDelegateSkillAdmissionDecided):
    """Admission booleans must be JSON booleans, never truthy strings."""

    admitted: StrictBool


class ModelClientTerminal(ModelDelegateSkillResponse):
    """Retain client echoes for the waiting adapter without changing any producer."""

    admitted: StrictBool
    effective_mode: EnumClientMaxMode
    client_call_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    quality_gate_passed: StrictBool
