# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Required contract parameters for named_symbols."""

from pydantic import BaseModel, ConfigDict, Field


class ModelNamedSymbolsParams(BaseModel):
    """Validated, caller-supplied criterion configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ignore_symbols: tuple[str, ...]
    ignore_python_builtins: bool
    max_missing_fraction: float = Field(ge=0, le=1)
