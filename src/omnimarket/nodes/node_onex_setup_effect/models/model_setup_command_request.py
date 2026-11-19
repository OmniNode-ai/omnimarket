# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A bounded process invocation with secrets confined to standard input."""

from pathlib import Path

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    SecretStr,
    field_validator,
)


class ModelSetupCommandRequest(BaseModel):
    """Validated argv, explicit environment and optional hidden stdin."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    argv: tuple[str, ...] = Field(min_length=1)
    cwd: Path | None = None
    stdin: SecretStr | None = Field(default=None, exclude=True, repr=False)
    timeout_seconds: FiniteFloat = Field(default=60.0, gt=0, le=7200)
    environment: dict[str, str] = Field(default_factory=dict)

    @field_validator("argv")
    @classmethod
    def valid_arguments(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value[0] or any("\0" in item for item in value):
            raise ValueError("command arguments must be valid strings")
        return value
