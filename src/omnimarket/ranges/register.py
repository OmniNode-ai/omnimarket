# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The check register's own check (unified plan row G1, AC1).

Every registered check declares its class, gate or range, and a range carries
its full acceptance line or an explicit NOT SET status. The check aggregates
over the entries it finds, so it refuses the zero-member cases too (plan
section 2d): an empty register, an entry with no id, a duplicate id.

Errors name the check they concern. A raw pre-pass runs before model
validation, so an entry with no class is reported by its check_id rather than
by a list index.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from pydantic import ValidationError

from omnimarket.models.ranges import (
    EnumCheckClass,
    EnumRangeStatus,
    ModelCheckDeclaration,
)
from omnimarket.ranges.power import required_sample_size

#: The committed register. Its path is package-relative, not machine-specific.
DEFAULT_CHECK_REGISTER_PATH = (
    Path(__file__).resolve().parent.parent / "configs" / "check_register.yaml"
)

_SCHEMA_VERSION = "check_register.v1"
_CLASSES = {member.value for member in EnumCheckClass}


def _entry_errors(index: int, entry: object) -> tuple[str | None, list[str]]:
    if not isinstance(entry, Mapping):
        return None, [f"checks[{index}] is not a mapping"]
    check_id = entry.get("check_id")
    if not isinstance(check_id, str) or not check_id.strip():
        return None, [f"checks[{index}] has no check_id"]
    check_class = entry.get("check_class")
    if check_class is None:
        return check_id, [
            f"{check_id}: declares no class; every check is a gate or a range"
        ]
    if check_class not in _CLASSES:
        return check_id, [
            f"{check_id}: class {check_class!r} is neither gate nor range"
        ]
    try:
        declaration = ModelCheckDeclaration.model_validate(entry)
    except ValidationError as exc:
        return check_id, [
            f"{check_id}: {'.'.join(str(p) for p in error['loc']) or 'entry'}: "
            f"{error['msg']}"
            for error in exc.errors()
        ]
    line = declaration.acceptance_line
    if declaration.range_status is EnumRangeStatus.DECLARED and line is not None:
        power_n = required_sample_size(
            floor=line.floor,
            margin=line.method.margin,
            confidence=line.method.confidence,
            power=line.method.power,
        )
        if line.method.sample_size < power_n:
            return check_id, [
                f"{check_id}: declared n={line.method.sample_size} was not sized by "
                f"the power analysis: required n={power_n}"
            ]
    return check_id, []


def validate_check_register(document: object) -> list[str]:
    """Every error in a register document. An empty list means it passes."""
    if not isinstance(document, Mapping):
        return ["the register is not a mapping"]
    errors: list[str] = []
    version = document.get("schema_version")
    if version != _SCHEMA_VERSION:
        errors.append(
            f"schema_version is {version!r}; the register check reads {_SCHEMA_VERSION!r}"
        )
    checks = document.get("checks")
    if not isinstance(checks, list) or not checks:
        errors.append("the register lists no checks: an empty register is refused")
        return errors
    seen: set[str] = set()
    for index, entry in enumerate(checks):
        check_id, entry_errors = _entry_errors(index, entry)
        errors.extend(entry_errors)
        if check_id is not None:
            if check_id in seen:
                errors.append(f"{check_id}: duplicate check_id")
            seen.add(check_id)
    return errors


__all__ = ["DEFAULT_CHECK_REGISTER_PATH", "validate_check_register"]
