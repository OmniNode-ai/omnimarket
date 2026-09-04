# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Deterministic preflight sanitization for ADR publication candidates.

The patterns are policy data in ``docs/adr-canary/sanitization_gate.yaml``.
This module deliberately does not duplicate them: public publication fails
before the publisher subprocess seam when the checked-in policy is malformed
or a blocking rule matches. The public KB's own validator is invoked again on
the rendered artifact before any git mutation.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from omnimarket.models.adr import (
    EnumAdrKBDestination,
    ModelAdrPublicationCandidate,
)

_SANITIZATION_CONFIG_PATH = (
    Path(__file__).resolve().parents[4]
    / "docs"
    / "adr-canary"
    / "sanitization_gate.yaml"
)


def validate_candidate_preflight(
    candidate: ModelAdrPublicationCandidate,
    destination: EnumAdrKBDestination,
) -> tuple[str, ...]:
    """Return deterministic blocking findings for a candidate's rendered surface.

    Only generated ADR fields are scanned. Hash-only provenance is publication
    evidence, not generated ADR content, and is validated separately by the
    publisher policy boundary.
    """
    try:
        raw = yaml.safe_load(_SANITIZATION_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return (f"SANITIZATION_CONFIG_UNREADABLE: {type(exc).__name__}",)
    if not isinstance(raw, dict):
        return ("SANITIZATION_CONFIG_INVALID: expected mapping",)
    checks = raw.get("sanitization_checks")
    if not isinstance(checks, list):
        return ("SANITIZATION_CONFIG_INVALID: sanitization_checks must be a list",)

    # Scan the typed values, not a JSON serialization.  JSON escaping would
    # turn a credential marker such as ``api_key: \"...\"`` into a sequence
    # containing literal backslashes and let the policy regex miss it.
    rendered_surface = "\n".join(
        _iter_text_values(candidate.draft.model_dump(mode="json"))
    )
    findings: list[str] = []
    for check in checks:
        if not isinstance(check, dict):
            return ("SANITIZATION_CONFIG_INVALID: check must be a mapping",)
        if check.get("severity") != "blocking":
            continue
        destinations = check.get("destinations", ["public", "private"])
        if not isinstance(destinations, list) or destination.value not in destinations:
            continue
        name = check.get("name")
        patterns = check.get("patterns")
        if not isinstance(name, str) or not name or not isinstance(patterns, list):
            continue
        for pattern in patterns:
            if not isinstance(pattern, str):
                return (f"SANITIZATION_CONFIG_INVALID: {name} pattern is not text",)
            try:
                if re.search(pattern, rendered_surface):
                    findings.append(name)
                    break
            except re.error as exc:
                return (f"SANITIZATION_CONFIG_INVALID: {name}: {exc}",)
    return tuple(findings)


def _iter_text_values(value: object) -> list[str]:
    """Flatten JSON-compatible generated fields without changing their text."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _iter_text_values(item)]
    if isinstance(value, list | tuple):
        return [text for item in value for text in _iter_text_values(item)]
    return []


__all__ = ["validate_candidate_preflight"]
