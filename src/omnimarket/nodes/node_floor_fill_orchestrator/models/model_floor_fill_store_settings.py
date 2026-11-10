# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The settings of the table-backed state store: every value is supplied, none has a default."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ModelFloorFillStoreSettings(BaseModel):
    """Where the ``floor_fill_state`` rows live and how long a connect may take.

    ``dsn`` is a secret read from the deployment's secret store by the binding site; it is never printed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dsn: SecretStr
    schema_name: str = Field(min_length=1)
    table_name: str = Field(min_length=1)
    connect_timeout_seconds: int = Field(gt=0)


__all__: list[str] = ["ModelFloorFillStoreSettings"]
