# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One knowledge store declared in the routing rules."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.kb_routing.enum_kb_store_kind import EnumKbStoreKind


class ModelKbStore(BaseModel):
    """A store: id, kind, audience, handler key, base path and the checks it requires.

    The audience is a string validated against the rules' audience order, not a
    closed enum, so another team's overlay can name its own audiences.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    kind: EnumKbStoreKind
    audience: str = Field(min_length=1)
    handler_key: str = Field(min_length=1)
    base_path: str = Field(min_length=1)
    required_checks: tuple[str, ...] = ()


__all__ = ["ModelKbStore"]
