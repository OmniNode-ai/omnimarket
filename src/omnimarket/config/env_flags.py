# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical fail-closed environment boolean flag resolver (OMN-14151).

``env_flag`` is the ONE way omnimarket resolves a boolean environment
variable that gates a side effect. An unset OR malformed value both resolve
to the caller-supplied ``safe_default`` — never to a "convenient" default —
so a typo (``"tru"``, ``"1 "``, an empty string) fails closed exactly like an
absent variable, instead of accidentally falling through to whichever branch
``bool(raw)`` happens to produce.

Usage::

    from omnimarket.config.env_flags import env_flag

    if not env_flag("OMNIMARKET_LEGACY_MERGE_ARM_ENABLED", safe_default=False):
        return _noop_result(...)
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def env_flag(name: str, *, safe_default: bool) -> bool:
    """Resolve a boolean env flag; unset or malformed both fall back to the SAFE value.

    Args:
        name: Environment variable name.
        safe_default: The value to use when ``name`` is unset OR set to a
            value that isn't recognized as true/false. Callers must supply
            the SAFE side for their use case (e.g. ``False`` to gate a
            mutation off by default), not a merely convenient one.

    Returns:
        True/False resolved from the env var, or ``safe_default``.
    """
    raw = os.environ.get(name)
    if raw is None:
        return safe_default
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    logger.warning(
        "env flag %s has malformed value %r; falling back to safe default %s",
        name,
        raw,
        safe_default,
    )
    return safe_default


__all__: list[str] = ["env_flag"]
