# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared score-vs-required-bar evidence helpers for delegation events."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

_LABEL_PREFIX = "p3."


def format_quality_bar_labels(
    *,
    required_bar: float,
    actual_score: float,
    escalation_count: int,
    authority_source: str,
    score_source: str,
    request_override_applied: bool,
    override_within_bounds: bool,
) -> list[str]:
    """Return compact event labels carrying P3 quality-bar evidence."""
    return [
        f"{_LABEL_PREFIX}required_bar={required_bar:.3f}",
        f"{_LABEL_PREFIX}actual_score={actual_score:.3f}",
        f"{_LABEL_PREFIX}escalation_count={escalation_count}",
        f"{_LABEL_PREFIX}authority_source={authority_source}",
        f"{_LABEL_PREFIX}score_source={score_source}",
        f"{_LABEL_PREFIX}request_override_applied={str(request_override_applied).lower()}",
        f"{_LABEL_PREFIX}override_within_bounds={str(override_within_bounds).lower()}",
    ]


def extract_quality_bar_evidence(
    payload: Mapping[str, object],
    *,
    checked_labels: Iterable[str] = (),
) -> dict[str, object]:
    """Extract P3 quality-bar evidence from explicit fields or event labels."""
    label_values = _label_values(checked_labels)
    evidence: dict[str, object] = {}

    required_bar = _first_float(payload, label_values, "required_bar", "requiredBar")
    if required_bar is not None:
        evidence["required_bar"] = required_bar

    actual_score = _first_float(
        payload,
        label_values,
        "actual_score",
        "actualScore",
        "quality_score",
        "qualityScore",
    )
    if actual_score is not None:
        evidence["actual_score"] = actual_score

    escalation_count = _first_int(
        payload,
        label_values,
        "escalation_count",
        "escalationCount",
    )
    if escalation_count is not None:
        evidence["escalation_count"] = escalation_count

    authority_source = _first_str(
        payload,
        label_values,
        "authority_source",
        "authoritySource",
        "required_bar_source",
        "requiredBarSource",
    )
    if authority_source is not None:
        evidence["authority_source"] = authority_source

    score_source = _first_str(payload, label_values, "score_source", "scoreSource")
    if score_source is not None:
        evidence["score_source"] = score_source

    request_override_applied = _first_bool(
        payload,
        label_values,
        "request_override_applied",
        "requestOverrideApplied",
    )
    if request_override_applied is not None:
        evidence["request_override_applied"] = request_override_applied

    override_within_bounds = _first_bool(
        payload,
        label_values,
        "override_within_bounds",
        "overrideWithinBounds",
    )
    if override_within_bounds is not None:
        evidence["override_within_bounds"] = override_within_bounds

    return evidence


def _label_values(labels: Iterable[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for label in labels:
        if not label.startswith(_LABEL_PREFIX):
            continue
        key, separator, value = label.removeprefix(_LABEL_PREFIX).partition("=")
        if separator and key:
            values[key] = value
    return values


def _first_value(
    payload: Mapping[str, object],
    label_values: Mapping[str, str],
    *keys: str,
) -> object | None:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return value
    for key in keys:
        value = label_values.get(key)
        if value is not None:
            return value
    return None


def _first_float(
    payload: Mapping[str, object],
    label_values: Mapping[str, str],
    *keys: str,
) -> float | None:
    value = _first_value(payload, label_values, *keys)
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        parsed = float(value)
    elif isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return None
    else:
        return None
    return parsed if math.isfinite(parsed) else None


def _first_int(
    payload: Mapping[str, object],
    label_values: Mapping[str, str],
    *keys: str,
) -> int | None:
    value = _first_float(payload, label_values, *keys)
    return int(value) if value is not None else None


def _first_bool(
    payload: Mapping[str, object],
    label_values: Mapping[str, str],
    *keys: str,
) -> bool | None:
    value = _first_value(payload, label_values, *keys)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return None


def _first_str(
    payload: Mapping[str, object],
    label_values: Mapping[str, str],
    *keys: str,
) -> str | None:
    value = _first_value(payload, label_values, *keys)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "extract_quality_bar_evidence",
    "format_quality_bar_labels",
]
