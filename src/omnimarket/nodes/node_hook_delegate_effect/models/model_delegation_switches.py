# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which delegation hook delegates (OMN-23413, operator 2026-11-15)."""

from pydantic import BaseModel, ConfigDict


class ModelDelegationSwitches(BaseModel):
    """Both hooks stay installed; a hook whose switch is off passes everything to
    Claude. Saved as ~/.onex/delegation.json."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    prompts: bool = True
    subagents: bool = True
