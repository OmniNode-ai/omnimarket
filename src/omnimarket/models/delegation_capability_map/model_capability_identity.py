# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The six-field evidence identity a capability cell is admissible under (OMN-18537)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

#: The six OMN-18537 identity fields, in their declared order.
IDENTITY_FIELDS: tuple[str, ...] = (
    "model_revision",
    "backend",
    "inference_configuration",
    "suite_revision",
    "grader_revision",
    "harness_revision",
)


class ModelCapabilityIdentity(BaseModel):
    """Model revision, backend, inference configuration, suite, grader and harness revision.

    A field is None when the producer did not record it; a row with any field
    None is inadmissible, which keeps a swapped model from inheriting a cell.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_revision: str | None = Field(default=None, min_length=1)
    backend: str | None = Field(default=None, min_length=1)
    inference_configuration: str | None = Field(default=None, min_length=1)
    suite_revision: str | None = Field(default=None, min_length=1)
    grader_revision: str | None = Field(default=None, min_length=1)
    harness_revision: str | None = Field(default=None, min_length=1)

    def missing_fields(self) -> tuple[str, ...]:
        """The identity fields this record does not carry."""
        return tuple(name for name in IDENTITY_FIELDS if getattr(self, name) is None)

    def differing_fields(self, other: ModelCapabilityIdentity) -> tuple[str, ...]:
        """The identity fields on which this record and ``other`` differ."""
        return tuple(
            name
            for name in IDENTITY_FIELDS
            if getattr(self, name) != getattr(other, name)
        )
