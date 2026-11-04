# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One page of the ``pr_state`` exposure, or the projection's refusal to serve it."""

from pydantic import BaseModel, ConfigDict

from omnimarket.models.pr_state.model_pr_state_row import ModelPrStateRow


class ModelPrStateProjectionPage(BaseModel):
    """The rows of one ``GET /projection/{topic}`` answer.

    ``truncated`` says the exposure holds rows the page left out, so a PR missing
    from ``rows`` is not thereby absent from the projection. ``cursor`` is the
    newest ``observed_at`` among the rows, as the projection wrote it: the value
    a later ``since`` read passes to receive only rows observed after this page.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    topic: str
    rows: tuple[ModelPrStateRow, ...]
    truncated: bool
    cursor: str | None = None


class ModelPrStateProjectionRefusal(BaseModel):
    """The projection could not serve a page; ``error`` is its fixed code.

    ``status_code`` is the HTTP status, or 0 when no answer arrived.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status_code: int
    error: str
