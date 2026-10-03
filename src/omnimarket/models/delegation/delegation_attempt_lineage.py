# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure lineage stamping for delegation attempt ladders (OMN-20168)."""

from collections.abc import Mapping, Sequence
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, UUID, uuid5

from omnimarket.enums.enum_delegation_attempt_kind import EnumDelegationAttemptKind

ATTEMPT_ID_NAMESPACE: UUID = uuid5(NAMESPACE_URL, "omnimarket:delegation-attempt")


def attempt_id_for(correlation_id: UUID, attempt_index: int) -> UUID:
    """Name one rung deterministically within a delegation."""
    if attempt_index < 0:
        raise ValueError("attempt_index must be nonnegative")
    return uuid5(ATTEMPT_ID_NAMESPACE, f"{correlation_id}:{attempt_index}")


def endpoint_host(endpoint_url: str) -> str | None:
    """The endpoint's lowercase hostname, or None for an unparseable endpoint."""
    try:
        host = urlparse(endpoint_url).hostname
        return host.lower() if host else None
    except (ValueError, TypeError, AttributeError):
        return None


def stamp_attempt_lineage(
    rungs: Sequence[Mapping[str, object]], *, correlation_id: UUID
) -> list[dict[str, object]]:
    """Copy the ladder, retaining producer lineage and filling whole-request defaults."""
    stamped: list[dict[str, object]] = []
    for index, rung in enumerate(rungs):
        entry = dict(rung)
        entry.setdefault("attempt_id", str(attempt_id_for(correlation_id, index)))
        entry.setdefault(
            "attempt_kind",
            EnumDelegationAttemptKind.FIRST_TRY.value
            if index == 0
            else EnumDelegationAttemptKind.WHOLE_ESCALATION.value,
        )
        entry.setdefault(
            "parent_attempt_id", None if index == 0 else stamped[-1]["attempt_id"]
        )
        entry.setdefault("split_id", None)
        entry.setdefault("host", None)
        entry.setdefault("size_band", None)
        for key in ("attempt_id", "parent_attempt_id", "split_id"):
            if isinstance(entry[key], UUID):
                entry[key] = str(entry[key])
        if isinstance(entry["attempt_kind"], EnumDelegationAttemptKind):
            entry["attempt_kind"] = entry["attempt_kind"].value
        stamped.append(entry)
    return stamped
