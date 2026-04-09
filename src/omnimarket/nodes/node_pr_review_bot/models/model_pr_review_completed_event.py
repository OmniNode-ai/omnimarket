# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from pydantic import BaseModel


class PRReviewCompletedEvent(BaseModel):
    pull_request_id: str
    success: bool
    message: str
