# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from pydantic import BaseModel


class PRReviewStartCommand(BaseModel):
    pull_request_id: str
    repository: str
