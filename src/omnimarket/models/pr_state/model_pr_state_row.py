# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One row of the ``pr_state`` projection, as the read surface reports it.

The columns are the ones the projection exposure declares. ``ci_verdict`` is the
verdict over the repository's required checks; the read surface reports it as
``required_verdict`` so a reader never has to guess which checks it covers.
"""

from datetime import datetime
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from omnimarket.events.pr_state import EnumPrState


class ModelPrStateRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    repo: str = Field(min_length=1)
    pr_number: int = Field(gt=0)
    state: EnumPrState
    head_sha: str
    base: str
    head_ref: str
    title: str
    draft: bool
    author: str
    author_is_bot: bool
    labels: tuple[str, ...]
    armed: bool
    queued: bool
    watcher_class: str
    required_verdict: Literal["GREEN", "RED", "PENDING", "NONE"] = Field(
        validation_alias=AliasChoices("required_verdict", "ci_verdict")
    )
    red_contexts: tuple[str, ...]
    pending_contexts: tuple[str, ...]
    ci_read_at: str
    merged_at: str
    observed_at: datetime
