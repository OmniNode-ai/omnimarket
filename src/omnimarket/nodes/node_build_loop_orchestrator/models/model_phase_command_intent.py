# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from pydantic import BaseModel


class PhaseCommandIntent(BaseModel):
    phase: str
    intent: str
    created_at: str
