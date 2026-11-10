# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The owner map: who the landing controller says holds each pull request and each shared red (OMN-23391).

The controller's host reads its own state file and publishes this map as the ``owner_map`` floor projection; the floor takes
the map from the projection and never from a copy of the file on the machine that publishes the page.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import JsonValue


def short_key(ref: str) -> str:
    """``owner/repo#n`` as ``repo#n``; a cause key (no ``#``) unchanged."""
    return ref.rpartition("/")[2] if "#" in ref else ref


class LaneOwners:
    """Who the landing controller says holds a pull request: a landing lease on it, or the lease of a cause that holds it."""

    def __init__(
        self, landing: dict[str, str], cause: dict[str, str], parked: dict[str, str]
    ):
        self.landing = landing
        self.cause = cause
        self.parked = parked

    def of(self, pr: str) -> tuple[str, str]:
        """(landing lease, cause lease) for ``repo#n``; a parked cause is named as the cause owner."""
        return self.landing.get(pr, ""), self.cause.get(pr) or self.parked.get(pr, "")

    def payload(self) -> dict[str, JsonValue]:
        """The map as the projection carries it."""
        return {
            "landing": dict(self.landing),
            "cause": dict(self.cause),
            "parked": dict(self.parked),
        }


def owners_from_payload(payload: dict[str, JsonValue] | None) -> LaneOwners | None:
    """The map from a projection's payload, or ``None`` when there is no payload or it is not a map."""
    if payload is None:
        return None
    parts: list[dict[str, str]] = []
    for name in ("landing", "cause", "parked"):
        part = payload.get(name)
        if not isinstance(part, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in part.items()
        ):
            return None
        parts.append({k: str(v) for k, v in part.items()})
    return LaneOwners(*parts)


def parse_controller_state(text: str) -> LaneOwners | None:
    """The controller's state file (``leases`` and ``causes``) as an owner map, or ``None`` when it is not a state."""
    try:
        state = json.loads(text)
        leases = state.get("leases") or []
        causes = state.get("causes") or []
        if not isinstance(leases, list) or not isinstance(causes, list):
            return None
        by_subject = {short_key(str(le["pr"])): int(le["lease_id"]) for le in leases}
        cause_keys = {str(c["key"]) for c in causes}
        landing: dict[str, str] = {}
        cause: dict[str, str] = {}
        parked: dict[str, str] = {}
        for subject, lease_id in by_subject.items():
            if subject not in cause_keys:
                landing[subject] = f"landing-L{lease_id}"
        for rec in causes:
            key = str(rec["key"])
            members = [short_key(str(m["pr"])) for m in rec.get("members") or ()]
            if key in by_subject:
                for member in members:
                    cause[member] = f"cause-L{by_subject[key]} {key}"
            elif rec.get("parked_until"):
                for member in members:
                    parked[member] = f"parked {key} until {rec['parked_until']}"
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return LaneOwners(landing, cause, parked)


def read_controller_owners(path: Path) -> LaneOwners | None:
    """The controller's state file as an owner map, or ``None`` when it is absent or not a state."""
    try:
        return parse_controller_state(path.read_text(encoding="utf-8"))
    except OSError:
        return None
