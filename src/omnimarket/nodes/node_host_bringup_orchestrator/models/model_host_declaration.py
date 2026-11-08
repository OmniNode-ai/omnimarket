# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models for one host declaration and its derived copies (OMN-21899).

A host is declared once, as its row in the owner copy of the lab host table.
Every other copy carries the fields its surface spec names, taken from that row,
and nothing else; the derive handler reports each place a copy differs.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue

_SURFACE = r"^[a-z][a-z0-9-]*$"
_HOST = r"^[a-z][a-z0-9-]*$"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumCopyDifferenceKind(StrEnum):
    MISSING_HOST = "missing_host"
    SURPLUS_HOST = "surplus_host"
    MISSING_FIELD = "missing_field"
    SURPLUS_FIELD = "surplus_field"
    VALUE_DIFFERS = "value_differs"
    DUPLICATE_HOST = "duplicate_host"


class ModelHostSurfaceSpec(_Frozen):
    """One derived copy of the host table and the row fields it carries."""

    surface: str = Field(pattern=_SURFACE)
    fields: tuple[str, ...] = Field(min_length=1)


class ModelHostDeclarationDeriveRequest(_Frozen):
    """The owner rows, the surface specs, each copy as read, and any host to flip."""

    declaration: tuple[dict[str, JsonValue], ...] = Field(min_length=1)
    surfaces: tuple[ModelHostSurfaceSpec, ...] = Field(min_length=1)
    copies: dict[str, tuple[dict[str, JsonValue], ...]] = Field(default_factory=dict)
    flip_in_service: tuple[str, ...] = ()


class ModelHostCopyDifference(_Frozen):
    surface: str
    host: str
    field: str | None = None
    kind: EnumCopyDifferenceKind
    have: str | None = None
    want: str | None = None


class ModelHostDeclarationDerived(_Frozen):
    declaration: tuple[dict[str, JsonValue], ...] = ()
    derived: dict[str, tuple[dict[str, JsonValue], ...]] = Field(default_factory=dict)
    differences: tuple[ModelHostCopyDifference, ...] = ()
    refusals: tuple[str, ...] = ()
    in_sync: bool = False


__all__ = [
    "EnumCopyDifferenceKind",
    "ModelHostCopyDifference",
    "ModelHostDeclarationDeriveRequest",
    "ModelHostDeclarationDerived",
    "ModelHostSurfaceSpec",
]
