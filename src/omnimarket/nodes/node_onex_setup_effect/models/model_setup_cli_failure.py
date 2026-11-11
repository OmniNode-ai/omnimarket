# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Secret-free typed failure at the setup command boundary."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelSetupCliFailure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["refused"] = "refused"
    error_code: Literal["invalid_setup_selection"] = "invalid_setup_selection"
    message: str
