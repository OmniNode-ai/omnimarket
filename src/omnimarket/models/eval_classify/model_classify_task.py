# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One classify eval task: a merged-change line with the label the integration pass gave it."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.eval_classify.enum_classify import EnumIntegrationClass


class ModelClassifyEvalTask(BaseModel):
    """A line the classify step is measured on.

    ``label`` is the class the ledger's own integration pass wrote for the line (the section it sits
    in). ``title`` is always shown to an engine; ``runs`` and ``evidence`` are shown only to an engine
    measured on the full input. The state, step and owner cells of the ledger line are never carried:
    they name the answer.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_kind: Literal["classify_integration_line"] = "classify_integration_line"
    task_id: str = Field(min_length=1)
    label: EnumIntegrationClass
    repository: str = Field(min_length=1)
    title: str = Field(min_length=1)
    merged_at: str
    runs: str
    evidence: str


__all__ = ["ModelClassifyEvalTask"]
