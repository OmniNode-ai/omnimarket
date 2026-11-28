# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The cost-and-throughput effect's request, result and the runtime block of its contract."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelFloorCostThroughputRequest(_Frozen):
    home: str | None = None


class ModelFloorCostThroughputResult(_Frozen):
    ok: bool
    wrote: str = ""
    detail: str = ""
    unreadable: int = Field(default=0, ge=0)


class ModelCostThroughputRuntime(_Frozen):
    """The contract's runtime block: where the documents are read and written, relative to the registry home."""

    home_env: str = Field(min_length=1)
    data_dir_relpath: str = Field(min_length=1)
    floor_simple_name: str = Field(min_length=1)
    delegation_history_name: str = Field(min_length=1)
    day_over_day_name: str = Field(min_length=1)
    readings_name: str = Field(min_length=1)
    output_name: str = Field(min_length=1)
    readings_keep_days: int = Field(gt=0)
