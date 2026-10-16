# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An open GitHub issue as the workflow lists it (OMN-20808)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

ISSUE_TITLE = (
    "External dead-man: the automation watchdog or its alarm path has gone quiet"
)


class ModelExternalDeadmanIssue(BaseModel):
    """The number, title and body of one open issue; only the title decides whether it is ours."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    number: int
    title: str
    body: str = ""
