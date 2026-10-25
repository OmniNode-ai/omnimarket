# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cache judging-path observations and ledger row indexes between controller invocations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidatorFunctionWrapHandler,
    field_validator,
)


class ModelLandingTickSpeedRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)
    operation: Literal["judge_touches", "ledger_indexed"]
    state_dir: Path | None = None
    timing: dict[str, Any] | None = None
    lf: Any = None
    snap: Any = None
    leases: tuple[dict[str, Any], ...] = ()
    results: tuple[dict[str, Any], ...] = ()
    prior: Mapping[str, Any] | None = None
    now: str = ""
    read_touch: Callable[..., dict[str, Any]] | None = None
    rows_cache: dict[Any, Any] | None = None
    read: Callable[[], Any] | None = None

    @field_validator("timing", "rows_cache", mode="wrap")
    @classmethod
    def preserve_mutable_buffers(
        cls, value: Any, handler: ValidatorFunctionWrapHandler
    ) -> Any:
        """Validate caller-owned buffers while retaining their in-place update semantics."""
        validated = handler(value)
        return value if isinstance(value, dict) else validated


class ModelLandingTickSpeedResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)
    value: Any
