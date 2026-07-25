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


class LegacyMergeArmDisabledError(RuntimeError):
    """Raised when a legacy merge-arm/auto-merge surface is invoked while gated off.

    OMN-15053: an agent that dogfoods ``/onex:merge_sweep`` (CLAUDE.md rule 1)
    can reach a real GitHub-mutating merge path even though CLAUDE.md rule 3
    says agents never merge — that rule had no mechanism behind it until now.
    ``omnimarket#1879`` landed this way on 2026-07-24 via
    ``node_merge_sweep_auto_merge_arm_effect``. Landing a PR through any of
    the legacy arm surfaces gated by ``OMNIMARKET_LEGACY_MERGE_ARM_ENABLED``
    (``node_merge_sweep_auto_merge_arm_effect``, ``node_auto_merge_effect``)
    is now refused LOUDLY (this exception) instead of the previous silent
    no-op result, so a disabled guard can never be mistaken for a guard that
    was simply never reached.
    """


def require_legacy_merge_arm_enabled(
    env_var_name: str, *, surface: str, context: str
) -> None:
    """Raise :class:`LegacyMergeArmDisabledError` unless explicitly re-enabled.

    Callers use this at the point of GitHub mutation (arming auto-merge or
    calling ``gh pr merge``) instead of a soft ``if not env_flag(...): return
    noop`` branch, so a disabled surface fails loudly and audibly rather than
    quietly reporting success with an easy-to-miss error string.

    Args:
        env_var_name: Name of the gating env var (e.g.
            ``OMNIMARKET_LEGACY_MERGE_ARM_ENABLED``).
        surface: Human-readable node/handler name, for the error message.
        context: Repo#PR (or similar) identifying what was about to be
            mutated, for the error message.

    Raises:
        LegacyMergeArmDisabledError: always, unless
            ``env_flag(env_var_name, safe_default=False)`` resolves True.
    """
    if env_flag(env_var_name, safe_default=False):
        return
    logger.error(
        "%s: REFUSING to arm/merge %s -- OMN-15053 kill switch engaged "
        "(%s is not enabled). This is a loud, intentional refusal, not an "
        "infra error -- see OMN-15053 for the re-enable path.",
        surface,
        context,
        env_var_name,
    )
    raise LegacyMergeArmDisabledError(
        f"{surface}: refusing to arm/merge {context} -- OMN-15053: the "
        "legacy merge-arm surface is disabled by operator decision "
        "('nothing should merge a PR right now'). "
        f"Set {env_var_name}=true to re-enable this surface -- that "
        "requires a fresh operator decision; see OMN-15053 for context "
        "and current status before flipping it."
    )


__all__: list[str] = [
    "LegacyMergeArmDisabledError",
    "env_flag",
    "require_legacy_merge_arm_enabled",
]
