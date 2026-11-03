# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the wait handler gets pages of the ``pr_state`` exposure."""

from typing import Protocol

from omnimarket.models.pr_state.model_pr_state_projection_page import (
    ModelPrStateProjectionPage,
    ModelPrStateProjectionRefusal,
)


class ProtocolPrStatePageSource(Protocol):
    def fetch(
        self, since: str | None
    ) -> ModelPrStateProjectionPage | ModelPrStateProjectionRefusal:
        """The newest rows, or only those observed after ``since`` when given."""
        ...
