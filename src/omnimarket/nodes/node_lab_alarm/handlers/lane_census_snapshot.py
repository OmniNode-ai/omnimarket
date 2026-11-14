# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane census snapshot's contract, moved from the old alarm script's census helpers (OMN-24425).

Moved unchanged from ``scripts/lane_census_event.py`` (``validate_event``, ``_alert_key``, the drift-event key
set and the supported schema versions) and ``scripts/lane_census_plan.py`` (``FINDING_KIND_SEVERITY``, the one
severity of every finding kind the census may carry). The lane_drift and container_unhealthy verbs read the
census snapshot through these. Parity with the old copies is pinned by
``tests/nodes/node_lab_alarm/test_lab_alarm_lane_census_parity.py``, whose expected values were computed by
running the old functions.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

#: Every kind the census event may carry, with its one severity.
FINDING_KIND_SEVERITY: dict[str, str] = {
    "container_absent": "critical",
    "replicas_zero": "critical",
    "network_detached": "critical",
    "oneshot_failed": "critical",
    "oneshot_stuck": "warning",
    "image_tag_mismatch": "warning",
    "unexpected_container": "warning",
    "profile_gated_present": "warning",
    "lane_on_undeclared_host": "critical",
    "revision_mismatch": "critical",
    "config_hash_mismatch": "critical",
    "package_version_mismatch": "critical",
    "container_unhealthy": "critical",
    "container_restart_loop": "critical",
    "undeclared_container": "warning",
    "broker_config_mismatch": "critical",
    "runner_count_mismatch": "warning",
    "runner_workdir_mismatch": "critical",
    "runner_offline": "warning",
    "desired_state_unreadable": "critical",
}

#: The versions ``validate_event`` accepts. 1.0.0 stays readable because snapshots written before the
#: 1.1.0 bump carry it.
SUPPORTED_EVENT_SCHEMA_VERSIONS = frozenset({"1.0.0", "1.1.0"})

#: The 1.0.0 drift-event key set and the JSON type of each value.
DRIFT_EVENT_FIELDS: dict[str, type | tuple[type, ...]] = {
    "schema_version": str,
    "event_type": str,
    "topic": str,
    "host": str,
    "host_id": (str, type(None)),
    "emitted_at": str,
    "severity": str,
    "lanes_checked": list,
    "lanes_not_applicable": list,
    "lanes_skipped_optional_down": list,
    "drift_count": int,
    "findings": list,
    "alert_key": str,
    "ticket_title": str,
    "ticket_body": str,
}

#: The five keys of every finding, all strings.
FINDING_FIELDS: tuple[str, ...] = ("lane", "kind", "container", "detail", "severity")


def alert_key(host: str, plan: Mapping[str, Any]) -> str:
    """Stable dedupe key over the (host, lane, kind, container) finding set."""
    signature = sorted(
        f"{f['lane']}:{f['kind']}:{f['container']}"
        for f in list(plan.get("findings", []))
    )
    digest = hashlib.sha256("|".join(signature).encode("utf-8")).hexdigest()[:16]
    return f"lane-census-drift:{host}:{digest}"


def validate_event(
    event: Mapping[str, Any], *, kind_severity: Mapping[str, str]
) -> list[str]:
    """Return every way ``event`` breaks the drift-event contract; empty is valid.

    ``kind_severity`` is ``FINDING_KIND_SEVERITY`` of this module, passed in so a test can
    declare its own table. A finding whose kind the table does not declare, or whose
    severity is not its kind's, is an error: the kind table is the contract.
    """
    errors: list[str] = []
    version = event.get("schema_version")
    if version not in SUPPORTED_EVENT_SCHEMA_VERSIONS:
        errors.append(
            f"schema_version {version!r} is not one of "
            f"{sorted(SUPPORTED_EVENT_SCHEMA_VERSIONS)}"
        )
    for key, expected in DRIFT_EVENT_FIELDS.items():
        if key not in event:
            errors.append(f"missing key {key!r}")
        elif not isinstance(event[key], expected) or isinstance(event[key], bool):
            errors.append(
                f"key {key!r} has type {type(event[key]).__name__}, expected {expected}"
            )
    extra = sorted(set(event) - set(DRIFT_EVENT_FIELDS))
    if extra:
        errors.append(f"keys outside the contract: {extra}")
    findings = event.get("findings")
    if not isinstance(findings, list):
        return errors
    if event.get("drift_count") != len(findings):
        errors.append(
            f"drift_count {event.get('drift_count')!r} != {len(findings)} findings"
        )
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            errors.append(f"findings[{index}] is not an object")
            continue
        for key in FINDING_FIELDS:
            if not isinstance(finding.get(key), str):
                errors.append(f"findings[{index}].{key} is not a string")
        kind = finding.get("kind")
        if not isinstance(kind, str) or kind not in kind_severity:
            errors.append(f"findings[{index}].kind {kind!r} is not a declared kind")
        elif finding.get("severity") != kind_severity[kind]:
            errors.append(
                f"findings[{index}] kind {kind!r} has severity "
                f"{finding.get('severity')!r}, declared {kind_severity[kind]!r}"
            )
    return errors


__all__ = [
    "DRIFT_EVENT_FIELDS",
    "FINDING_FIELDS",
    "FINDING_KIND_SEVERITY",
    "SUPPORTED_EVENT_SCHEMA_VERSIONS",
    "alert_key",
    "validate_event",
]
