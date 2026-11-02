# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Result of one announcer run (OMN-21597)."""

from omnibase_core.enums.enum_announce_refusal_reason import EnumAnnounceRefusalReason
from pydantic import BaseModel, ConfigDict, Field


class ModelAnnounceReport(BaseModel):
    """Names announced, withdrawn and refused by one run, with the typed reasons."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    announced: tuple[str, ...] = ()
    withdrawn: tuple[str, ...] = ()
    refused: dict[str, EnumAnnounceRefusalReason] = Field(default_factory=dict)
