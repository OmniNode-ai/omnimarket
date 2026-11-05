# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result for the overlay resolver node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class ModelOverlayResolveRequest(BaseModel):
    """Resolve the overlay of one skill.

    ``skill`` is the skill name; a hyphenated name maps to its underscore overlay id. ``roots`` are the overlay
    roots to search, in order; empty means the roots named by ``ONEX_SKILL_OVERLAY_ROOTS``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    skill: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    roots: tuple[str, ...] = ()


class ModelOverlayResolveResult(BaseModel):
    """The overlay that validated against its schema.

    ``overlay_id`` is the underscore id, ``overlay_path`` the ``overlay.yaml`` read, ``schema_path`` the
    ``overlay.schema.json`` it was validated against, ``document`` the overlay itself.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay_id: str
    overlay_path: str
    schema_path: str
    document: dict[str, JsonValue]
