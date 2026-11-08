# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract's smallest size band that may reach a paid rung (OMN-23180).

``task_class_contracts.v1.yaml`` declares ``paid_from_size_band`` (S, M or L) at
the top level as the default every class inherits, and a class may override it in
``escalation_policy.paid_from_size_band``. Both models keep unknown keys, so the
value is read from there and parsed into the band enum here.
"""

from __future__ import annotations

from functools import lru_cache

import yaml

from omnimarket.inference.task_class_authority import (
    ModelTaskClassAuthority,
    load_task_class_authority,
)
from omnimarket.models.delegation.model_size_band import EnumSizeBand

_KEY = "paid_from_size_band"


@lru_cache(maxsize=1)
def _loaded_authority() -> ModelTaskClassAuthority:
    """Load the task-class authority once; a raised read is never cached."""
    return load_task_class_authority()


def _band(declared: str | None, where: str) -> EnumSizeBand | None:
    """Parse one declared value; a value that is not S, M or L is a contract error."""
    if declared is None:
        return None
    try:
        return EnumSizeBand(declared)
    except ValueError as exc:
        raise ValueError(
            f"{where}: {_KEY} must be one of "
            f"{', '.join(b.value for b in EnumSizeBand)}, got {declared!r}"
        ) from exc


def paid_from_size_band_for(
    authority: ModelTaskClassAuthority, task_class: str
) -> EnumSizeBand | None:
    """The class's paid-rung band, else the inherited default, else ``None``."""
    entry = authority.task_classes.get(task_class)
    policy = entry.escalation_policy if entry is not None else None
    if policy is not None:
        own = _band(
            (policy.model_extra or {}).get(_KEY),
            f"task_classes.{task_class}.escalation_policy",
        )
        if own is not None:
            return own
    return _band((authority.model_extra or {}).get(_KEY), "top level")


def resolve_paid_from_size_band(task_class: str) -> EnumSizeBand | None:
    """The smallest size band that may reach a paid rung, or ``None`` when none is declared.

    ``None`` means the contract declares no such rule for the class, or the
    authority is unreadable this call (the next call reads it again); routing then
    applies none.
    """
    try:
        authority = _loaded_authority()
    except (OSError, ValueError, yaml.YAMLError):
        return None
    return paid_from_size_band_for(authority, task_class)


__all__ = ["paid_from_size_band_for", "resolve_paid_from_size_band"]
