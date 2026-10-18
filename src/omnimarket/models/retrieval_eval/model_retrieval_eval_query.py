# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One ranked retrieval result and its relevance labels (OMN-21132, plan task TC.9, port item AP-E1)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelRetrievalEvalQuery(BaseModel):
    """One ranked retrieval result and its relevance labels."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query_id: str = Field(min_length=1)
    relevant_ids: tuple[str, ...] = Field(min_length=1)
    retrieved_ids: tuple[str, ...] = ()
    failed: bool = False
    failure_reason: str | None = None
    slices: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_query(self) -> ModelRetrievalEvalQuery:
        for label, ids in (
            ("relevant_ids", self.relevant_ids),
            ("retrieved_ids", self.retrieved_ids),
            ("slices", self.slices),
        ):
            if len(ids) != len(set(ids)):
                raise ValueError(f"{label} must not contain duplicates")
        if self.failed:
            if self.retrieved_ids:
                raise ValueError("failed queries require empty retrieved_ids")
            if self.failure_reason is None or not self.failure_reason.strip():
                raise ValueError("failed queries require a non-empty failure_reason")
        elif self.failure_reason is not None:
            raise ValueError("successful queries require failure_reason to be None")
        return self
