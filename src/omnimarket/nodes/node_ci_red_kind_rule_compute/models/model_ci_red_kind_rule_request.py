# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelCiRedKindRuleRequest(BaseModel):
    """One failed check's name, and its job log lines, to classify against the overlay's rule table.

    ``log_excerpt`` is the log lines around the failed job's error lines, as the caller read
    them; it is empty when the caller has no log, and then no log pattern matches.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_name: str = Field(..., min_length=1)
    log_excerpt: str = Field(default="")


__all__: list[str] = ["ModelCiRedKindRuleRequest"]
