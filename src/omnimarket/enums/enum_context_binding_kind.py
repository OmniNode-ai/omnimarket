"""How a context profile is bound into the client that consumes its pack."""

from __future__ import annotations

from enum import StrEnum


class EnumContextBindingKind(StrEnum):
    """Binding kinds a profile may declare (plan section 4.1 item 5, 4.5)."""

    REPLACE = "replace"
    ADDITIVE = "additive"
    INLINE = "inline"


__all__ = ["EnumContextBindingKind"]
