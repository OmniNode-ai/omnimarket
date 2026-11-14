# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane-census bus events: their topics, key sets, builders and validator (OMN-24425).

Moved from ``scripts/lane_census_event.py`` (omnibase_infra, OMN-13011, OMN-18769, OMN-19088) and
``FINDING_KIND_SEVERITY`` from ``scripts/lane_census_plan.py``. This module is the one writer of the
census snapshot's shape on the omnimarket side: the lane_drift and container_unhealthy alarm verbs read the
snapshot through it (``node_lab_alarm.handlers.lane_census_snapshot`` re-exports it), and the census node
builds its events with it. Parity with the old functions is pinned by
``tests/models/test_lane_census_event.py`` and ``tests/nodes/node_lab_alarm/test_lab_alarm_lane_census_parity.py``,
whose expected values were computed by running the old functions.

The drift event is the alert authority: a downstream consumer opens the ticket naming exactly what is missing
or extra. The ``alert_key`` deduplicates one open ticket per (host, lane, kind-set).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

#: 1.1.0 widens the finding ``kind`` vocabulary with the eleven lab-sync kinds and changes nothing else:
#: every 1.0.0 key is present with its 1.0.0 type, and no key is added.
EVENT_SCHEMA_VERSION = "1.1.0"

#: The versions ``validate_event`` accepts. 1.0.0 stays readable because snapshots written before the
#: 1.1.0 bump carry it.
SUPPORTED_EVENT_SCHEMA_VERSIONS = frozenset({"1.0.0", EVENT_SCHEMA_VERSION})

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

# A finding is a flat string mapping (lane, kind, container, detail, severity); the plan envelope
# round-trips through JSON, so it is loosely typed and narrowed locally.
Finding = Mapping[str, str]
Plan = Mapping[str, Any]


def _findings(plan: Plan) -> list[Finding]:
    return list(plan.get("findings", []))


def alert_key(host: str, plan: Plan) -> str:
    """Stable dedupe key over the (host, lane, kind, container) finding set."""
    signature = sorted(
        f"{f['lane']}:{f['kind']}:{f['container']}" for f in _findings(plan)
    )
    digest = hashlib.sha256("|".join(signature).encode("utf-8")).hexdigest()[:16]
    return f"lane-census-drift:{host}:{digest}"


def ticket_title(plan: Plan) -> str:
    """One-line title naming exactly what is wrong."""
    findings = _findings(plan)
    if not findings:
        return "lane census: no drift"
    lanes = sorted({f["lane"] for f in findings})
    kinds: dict[str, int] = {}
    for f in findings:
        kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
    kind_summary = ", ".join(
        f"{count}x {kind}" for kind, count in sorted(kinds.items())
    )
    return f"fix(infra): lane drift [{', '.join(lanes)}] — {kind_summary}"


def ticket_body(host: str, plan: Plan) -> str:
    """The ticket body: every finding, then the action."""
    findings = _findings(plan)
    lanes_checked: list[str] = list(plan.get("lanes_checked", []))
    lines = [
        "## Lane census drift detected",
        "",
        f"Host: `{host}`",
        f"Lanes checked: {', '.join(lanes_checked)}",
        f"Lanes not applicable on this host: "
        f"{', '.join(plan.get('lanes_not_applicable', [])) or '(none)'}",
        "",
        "The desired-state lane census (the private lane-manifest.yaml)",
        "does not match the live runtime. Drift items below name exactly what is",
        "missing or extra. This is the regression class the lane-census ratchet",
        "(OMN-13011) exists to catch — it would have fired on 2026-06-11 when prod",
        "runtime containers and the broker network were silently absent.",
        "",
        "## Findings",
        "",
    ]
    for f in findings:
        lines.append(
            f"- **[{f['severity']}] {f['kind']}** `{f['container']}` ({f['lane']}): {f['detail']}"
        )
    lines.append("")
    lines.append("## Action")
    lines.append(
        "Bring the lane back to declared state (compose up the absent containers / "
        "reattach the network), or update the lane manifest in the same PR if the "
        "desired state legitimately changed. Do not silence the check."
    )
    return "\n".join(lines)


def build_event(
    *,
    host: str,
    plan: Plan,
    topic: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Construct the typed lane-census-drift event payload.

    ``topic`` is required: the drift topic is declared by the census collector's contract, never spelled
    in this module (the old script's default is a topic spelling the topic-naming lint refuses).
    """
    now = now or datetime.now(UTC)
    findings = _findings(plan)
    severity = (
        "critical" if any(f["severity"] == "critical" for f in findings) else "warning"
    )
    return {
        "schema_version": EVENT_SCHEMA_VERSION,
        "event_type": "lane-census-drift",
        "topic": topic,
        "host": host,
        "host_id": plan.get("host"),
        "emitted_at": now.isoformat(),
        "severity": severity,
        "lanes_checked": plan.get("lanes_checked", []),
        "lanes_not_applicable": plan.get("lanes_not_applicable", []),
        "lanes_skipped_optional_down": plan.get("lanes_skipped_optional_down", []),
        "drift_count": len(findings),
        "findings": findings,
        "alert_key": alert_key(host, plan),
        "ticket_title": ticket_title(plan),
        "ticket_body": ticket_body(host, plan),
    }


def build_observed_event(
    *,
    host: str,
    plan: Plan,
    topic: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Construct the census-OBSERVED event, drift or no drift.

    ``topic`` is required (the census contract declares it): a topic that carries only drift cannot tell
    "the lane matches its manifest" from "the census has not run for two days", so the census publishes this
    event on every run.

    A statement that the census RAN and what it saw, not that something is wrong: it carries no
    ``alert_key``, ``ticket_title`` or ``ticket_body``. ``observed_at`` (not ``emitted_at``) is the
    instant the census looked, so a republisher cannot restamp a stale observation as current.
    """
    now = now or datetime.now(UTC)
    findings = _findings(plan)
    return {
        "schema_version": EVENT_SCHEMA_VERSION,
        "event_type": "lane-census-observed",
        "topic": topic,
        "host": host,
        "host_id": plan.get("host"),
        "observed_at": now.isoformat(),
        "lanes_checked": plan.get("lanes_checked", []),
        "lanes_not_applicable": plan.get("lanes_not_applicable", []),
        "lanes_skipped_optional_down": plan.get("lanes_skipped_optional_down", []),
        "drift_count": len(findings),
        "findings": findings,
    }


def validate_event(
    event: Mapping[str, Any], *, kind_severity: Mapping[str, str]
) -> list[str]:
    """Return every way ``event`` breaks the drift-event contract; empty is valid.

    ``kind_severity`` is ``FINDING_KIND_SEVERITY`` of this module, passed in so a test can declare its own
    table. A finding whose kind the table does not declare, or whose severity is not its kind's, is an
    error: the kind table is the contract.
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
    "EVENT_SCHEMA_VERSION",
    "FINDING_FIELDS",
    "FINDING_KIND_SEVERITY",
    "SUPPORTED_EVENT_SCHEMA_VERSIONS",
    "alert_key",
    "build_event",
    "build_observed_event",
    "ticket_body",
    "ticket_title",
    "validate_event",
]
