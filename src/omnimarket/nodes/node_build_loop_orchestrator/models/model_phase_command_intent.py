# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from pydantic import BaseModel


class PhaseCommandIntent(BaseModel):
    phase_id: str
    command: str
    intent_time: str | None = None
