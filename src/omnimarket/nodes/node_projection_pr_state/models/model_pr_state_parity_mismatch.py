# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One named PR parity discrepancy."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelPrStateParityMismatch(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    kind: Literal[
        "in-file-not-in-projection", "in-projection-open-not-in-file", "field-mismatch"
    ]
    key: str
    field: str = ""
    expected: str | bool | tuple[str, ...] | None = None
    actual: str | bool | tuple[str, ...] | None = None
