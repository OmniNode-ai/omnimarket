# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A non-empty evaluation batch with unique query identifiers (OMN-21132, plan task TC.9, port item AP-E1)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.retrieval_eval.model_retrieval_eval_query import (
    ModelRetrievalEvalQuery,
)


class ModelRetrievalEvalRequest(BaseModel):
    """A non-empty evaluation batch with unique query identifiers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    eval_id: str = Field(min_length=1)
    queries: tuple[ModelRetrievalEvalQuery, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_query_ids(self) -> ModelRetrievalEvalRequest:
        ids = [query.query_id for query in self.queries]
        if len(ids) != len(set(ids)):
            raise ValueError("queries must not contain duplicate query_id values")
        return self
