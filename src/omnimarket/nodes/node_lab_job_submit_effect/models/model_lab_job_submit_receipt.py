# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Receipt of a lab job spec published to the supervisor's command topic."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelLabJobSubmitReceipt(BaseModel):
    """Publication status, job identity and the topic that accepted the command."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["published"]
    job_id: str
    topic: str
