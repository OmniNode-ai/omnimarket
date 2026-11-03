# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the wait handler gets pages of the ``pr_state`` exposure."""

from typing import Protocol

from omnimarket.models.pr_state.model_pr_state_page_fetch import (
    ModelPrStatePageRequest,
    ModelPrStatePageResult,
)


class ProtocolPrStatePageSource(Protocol):
    def handle(self, request: ModelPrStatePageRequest) -> ModelPrStatePageResult:
        """The newest rows, or only those observed after ``request.since``."""
        ...
