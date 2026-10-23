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


def _is_config_swap(
    entry: Mapping[str, object], previous: Mapping[str, object]
) -> bool:
    """The same backend and model retried with another configuration."""
    configuration = entry.get("configuration")
    return (
        configuration is not None
        and previous.get("configuration") is not None
        and configuration != previous.get("configuration")
        and entry.get("backend_id") == previous.get("backend_id")
        and entry.get("model_id") == previous.get("model_id")
    )


def stamp_attempt_lineage(
    rungs: Sequence[Mapping[str, object]],
    *,
    correlation_id: UUID,
    size_band: str | None = None,
    configuration: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    """Copy the ladder, retaining producer lineage and filling whole-request defaults.

    ``size_band`` and ``configuration`` are the request's own, filled only
    where a rung does not carry one. A rung redelivered with an attempt id
    already in the ladder is the same attempt, and is kept once.
    """
    stamped: list[dict[str, object]] = []
    seen: set[str] = set()
    for rung in rungs:
        index = len(stamped)
        entry = dict(rung)
        entry.setdefault("attempt_id", str(attempt_id_for(correlation_id, index)))
        if isinstance(entry["attempt_id"], UUID):
            entry["attempt_id"] = str(entry["attempt_id"])
        if entry["attempt_id"] is not None and entry["attempt_id"] in seen:
            continue
        if configuration is not None:
            entry.setdefault("configuration", dict(configuration))
        entry.setdefault(
            "attempt_kind",
            EnumDelegationAttemptKind.FIRST_TRY.value
            if index == 0
            else EnumDelegationAttemptKind.CONFIG_SWAP.value
            if _is_config_swap(entry, stamped[-1])
            else EnumDelegationAttemptKind.WHOLE_ESCALATION.value,
        )
        entry.setdefault(
            "parent_attempt_id", None if index == 0 else stamped[-1]["attempt_id"]
        )
        entry.setdefault("split_id", None)
        entry.setdefault("host", None)
        entry.setdefault("size_band", size_band)
        for key in ("parent_attempt_id", "split_id"):
            if isinstance(entry[key], UUID):
                entry[key] = str(entry[key])
        if isinstance(entry["attempt_kind"], EnumDelegationAttemptKind):
            entry["attempt_kind"] = entry["attempt_kind"].value
        if entry["attempt_id"] is not None:
            seen.add(str(entry["attempt_id"]))
        stamped.append(entry)
    return stamped
