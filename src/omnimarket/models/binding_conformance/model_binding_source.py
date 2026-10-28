# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One shipped file of a checked node, as text."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.binding_conformance.enum_binding_source_kind import (
    EnumBindingSourceKind,
)


class ModelBindingSource(BaseModel):
    """The caller reads the file; the check sees only its path and text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str = Field(min_length=1)
    path: str = Field(min_length=1)
    kind: EnumBindingSourceKind
    text: str
