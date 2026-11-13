# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request of the host table keys read: it carries no value, the overlay names every setting."""

from pydantic import BaseModel, ConfigDict


class ModelHostTableKeysReadRequest(BaseModel):
    """One read of the host table keys."""

    model_config = ConfigDict(frozen=True, extra="forbid")
