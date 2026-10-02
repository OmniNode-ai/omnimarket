# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic entry-tier traffic shares for task-class ladders (OMN-20163)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence

from omnibase_infra.errors import ProtocolConfigurationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ModelEntryTierShare(BaseModel):
    """The free rung offered first to a declared fraction of correlation ids."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: str = Field(min_length=1)
    share: float = Field(ge=0.0, le=1.0)


def entry_share_from_contract_entry(
    entry: dict[str, object] | None,
) -> ModelEntryTierShare | None:
    """Read an optional share, refusing malformed declarations."""
    if entry is None or "escalation_policy" not in entry:
        return None
    policy = entry["escalation_policy"]
    if policy is None:
        return None
    if not isinstance(policy, dict):
        raise ProtocolConfigurationError(
            "Invalid escalation_policy for entry_share (OMN-20163)."
        )
    if "entry_share" not in policy:
        return None
    try:
        share = ModelEntryTierShare.model_validate(policy["entry_share"])
    except ValidationError as exc:
        raise ProtocolConfigurationError(
            f"Invalid escalation_policy.entry_share (OMN-20163): {exc}"
        ) from exc
    return None if share.share == 0.0 else share


def entry_share_draw(correlation_id: object, share: float) -> bool:
    """Draw from a stable, domain-separated hash, independent of backend spread."""
    if share <= 0.0:
        return False
    if share >= 1.0:
        return True
    digest = hashlib.sha256(f"entry-tier-share:{correlation_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 < share


def entry_first_order(tier_names: Sequence[str], entry_tier: str) -> tuple[str, ...]:
    """Move the entry rung first, preserving the order of every other rung."""
    return (entry_tier, *(name for name in tier_names if name != entry_tier))


def validate_entry_share(
    share: ModelEntryTierShare,
    tier_order: Sequence[str],
    is_free: Callable[[str], bool],
) -> None:
    """Refuse an entry rung outside the ladder, already first, or metered."""
    if share.tier not in tier_order:
        raise ProtocolConfigurationError(
            f"entry_share tier {share.tier!r} is not in tier_order (OMN-20163)."
        )
    if share.tier == tier_order[0]:
        raise ProtocolConfigurationError(
            f"entry_share tier {share.tier!r} is already first in tier_order "
            "(OMN-20163)."
        )
    if not is_free(share.tier):
        raise ProtocolConfigurationError(
            f"entry_share tier {share.tier!r} must be free (OMN-20163)."
        )
