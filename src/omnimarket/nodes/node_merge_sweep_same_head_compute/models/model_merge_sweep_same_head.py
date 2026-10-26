# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the merge-sweep same-head skip (OMN-20864).

Frozen Pydantic models reject undeclared fields.
"""

from collections.abc import Mapping, Set
from typing import Any

from pydantic import BaseModel, ConfigDict


class ModelMergeSweepSameHeadRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    head: Mapping[str, Any]
    key: str
    scope: Set[str]


class ModelMergeSweepSameHeadResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    skipped: dict[str, Any] | None = None
