# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Guard loose receipt values so a bad reading never becomes an exception."""

import math
from collections.abc import Mapping

from .helpers_js_value import UNDEFINED, js_str, num, truthy


def obj(value: object) -> Mapping[str, object]:
    """A missing nested object has no fields, as in the workflow's optional reads."""
    return value if isinstance(value, Mapping) else {}


def rows(value: object) -> list[Mapping[str, object]]:
    """Only objects can carry reader fields; malformed entries remain empty readings."""
    return [obj(v) for v in value] if isinstance(value, (tuple, list)) else []


def string(value: object) -> str:
    """String() also handles container values a malformed JSON reading may carry."""
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity"
    if value is UNDEFINED:
        return "undefined"
    if isinstance(value, Mapping):
        return "[object Object]"
    if isinstance(value, (list, tuple)):
        return ",".join("" if v is None else string(v) for v in value)
    return js_str(value)


def text(value: object) -> str:
    """String(value || '') keeps JavaScript truthiness, including empty objects."""
    return string(value) if truthy(value) else ""


def number(value: object) -> float:
    """Number(value) || 0 for counts and rankings, using the shared coercion rule."""
    result = num(string(value) if isinstance(value, (list, tuple)) else value)
    return result or 0


def by_lane(values: object) -> dict[str, Mapping[str, object]]:
    """Last returned entry wins, matching the workflow's Map construction."""
    return {text(v.get("lane")): v for v in rows(values) if truthy(v.get("lane"))}


def pick_host(hosts: object, limited: tuple[str, ...]) -> str:
    """The first ranked non-limited host is the live fallback choice."""
    return (
        next((h for h in hosts if isinstance(h, str) and h and h not in limited), "")
        if isinstance(hosts, (list, tuple))
        else ""
    )
