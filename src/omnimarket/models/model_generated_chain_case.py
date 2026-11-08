# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One generated test case and the break it must catch (OMN-22892)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelGeneratedChainCase(BaseModel):
    """One (case, named break) pair of the generated test module.

    The generated module asserts the case, then injects ``named_break`` and asserts the
    case turns red; a case that names no break yields no pair and no test.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_name: str = Field(..., min_length=1)
    kind: Literal["GOLDEN", "ERROR"]
    named_break: str = Field(..., min_length=1)


__all__ = ["ModelGeneratedChainCase"]
