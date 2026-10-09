# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Value rules of the lab-fill workflow's readers, kept so decisions match it exactly (OMN-20668).

The enumerate read and the runner's placement read print loosely typed JSON. The
workflow judged it with JavaScript truthiness and Number(); these helpers do the
same, so a malformed reading or candidate is decided the way the workflow decided it.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping


class _Undefined:
    """JavaScript undefined: a key the reading did not carry, unlike a key it set to null."""

    def __repr__(self) -> str:
        return "undefined"


UNDEFINED = _Undefined()


def truthy(value: object) -> bool:
    """JavaScript truthiness: empty containers are truthy, 0, NaN, '' and null are not."""
    if value is None or value is False:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return value != 0 and not (isinstance(value, float) and math.isnan(value))
    if isinstance(value, str):
        return value != ""
    return True


def num(value: object) -> float | None:
    """Number(value) when finite, else None. null reads as 0, undefined as NaN."""
    if value is UNDEFINED:
        return None
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return 0.0
        try:
            number = float(text)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def is_finite_number(value: object) -> bool:
    """Number.isFinite: true only for a finite number, never for a string or a bool."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def is_integer(value: object) -> bool:
    """Number.isInteger."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and float(value) == math.floor(value)
    )


def js_str(value: object) -> str:
    """String(value) for the JSON scalars a reading can carry."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value == math.floor(value) and math.isfinite(value):
        return str(int(value))
    return str(value)


def text_of(value: object) -> str:
    """String(value || '')."""
    return js_str(value) if truthy(value) else ""


def round_half_up(value: float) -> float:
    """Math.round: halves go toward positive infinity, unlike Python's round."""
    return float(math.floor(value + 0.5))


def get(item: object, key: str) -> object:
    """item[key] for a mapping, undefined (None) for anything else."""
    return item.get(key) if isinstance(item, Mapping) else None


def has_word(text: object, word: str) -> bool:
    """Whole-word, case-insensitive match."""
    pattern = r"(^|[^a-z0-9])" + re.escape(word) + r"([^a-z0-9]|\Z)"
    return re.search(pattern, text_of(text), re.IGNORECASE) is not None


def has_word_stem(text: object, word: str) -> bool:
    """A word that starts with the term, case-insensitive."""
    pattern = r"(^|[^a-z0-9])" + re.escape(word)
    return re.search(pattern, text_of(text), re.IGNORECASE) is not None


def words(values: object) -> list[str]:
    """Trimmed, lowercased, non-blank strings of a list; anything else is empty."""
    if not isinstance(values, list | tuple):
        return []
    return [v.strip().lower() for v in values if isinstance(v, str) and v.strip()]
