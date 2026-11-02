# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seeded blocks and a projection-replayed, experiment-wide kill switch."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from ..models.model_arm_assignment import (
    Arm,
    IncidentKey,
    ModelArmAssignmentRequest,
    ModelArmAssignmentResult,
    ModelAttributedIncident,
    ModelBoundaryPackageEvidence,
    ModelExperimentMarker,
    ModelHookProjectionEvent,
    ModelIncidentSignal,
    ModelInstalledBoundary,
    ModelPackBuildManifest,
    ModelSessionArm,
)


def _order(
    row: ModelHookProjectionEvent | ModelInstalledBoundary | ModelIncidentSignal,
) -> tuple[datetime, str]:
    return row.event_time, row.event_id


def _incident_key(experiment: str, row: ModelIncidentSignal) -> IncidentKey:
    return experiment, row.session_id, row.boundary_uuid, row.item_key


def _is_incident_kind(kind: str) -> bool:
    return kind in {
        "act_against_ruling",
        "act_against_hold",
        "act_against_scope",
        "contradicted_decision",
    }


def _kill_switch_reached(keys: set[IncidentKey]) -> bool:
    return len(keys) >= 2


def _block_arm(seed: str, experiment: str, block: int, position: int) -> Arm:
    encoded = json.dumps([seed, experiment, block], separators=(",", ":")).encode()
    first = hashlib.sha256(encoded).digest()[0] & 1
    return "on" if (first ^ position) == 0 else "off"


def _safe_cell(value: str) -> str:
    return value.replace("|", "/").replace("\n", " ").replace("\r", " ")


class HandlerCompactionArmAssignment:
    """Pure full-projection fold; marker/ledger rows are effect intents.

    A reader supplies the complete experiment projection on each call. Earlier
    markers retain their exposure arm; current bindings and later markers become
    off when the switch trips. Callers persist markers by event_id and the single
    FRICTION row by its deterministic experiment key, through existing effects.
    """

    def handle(self, request: ModelArmAssignmentRequest) -> ModelArmAssignmentResult:
        first: dict[str, ModelHookProjectionEvent] = {}
        events: dict[str, ModelHookProjectionEvent] = {}
        for event in request.hook_events:
            if event.event_id in events and events[event.event_id] != event:
                raise ValueError("conflicting hook event identity")
            events[event.event_id] = event
        for event in sorted(events.values(), key=_order):
            if event.role == "orchestrator":
                first.setdefault(event.session_id, event)
        sessions = tuple(first.values())
        base_arms = {
            event.session_id: _block_arm(
                request.seed, request.experiment_id, i // 2, i % 2
            )
            for i, event in enumerate(sessions)
        }
        boundaries: dict[tuple[str, str], ModelInstalledBoundary] = {}
        for installed in request.boundaries:
            key = installed.session_id, installed.boundary_uuid
            if key in boundaries and boundaries[key] != installed:
                raise ValueError("conflicting boundary identity")
            boundaries[key] = installed
        manifests: dict[tuple[str, str], ModelPackBuildManifest] = {}
        conflicting_manifests: set[tuple[str, str]] = set()
        for build in request.build_manifests:
            manifest_key = build.package_id, build.installed_messages_hash
            if manifest_key in manifests and manifests[manifest_key] != build:
                conflicting_manifests.add(manifest_key)
            manifests[manifest_key] = build
        for manifest_key in conflicting_manifests:
            del manifests[manifest_key]

        incidents: list[ModelAttributedIncident] = []
        keys: set[IncidentKey] = set()
        trip: ModelIncidentSignal | None = None
        signal_events: dict[str, ModelIncidentSignal] = {}
        for signal in request.signals:
            if (
                signal.event_id in signal_events
                and signal_events[signal.event_id] != signal
            ):
                raise ValueError("conflicting incident event identity")
            signal_events[signal.event_id] = signal
        for signal in sorted(signal_events.values(), key=_order):
            boundary = boundaries.get((signal.session_id, signal.boundary_uuid))
            session = first.get(signal.session_id)
            if (
                boundary is None
                or session is None
                or base_arms.get(signal.session_id) != "on"
                or _order(boundary) < _order(session)
                or _order(signal) <= _order(boundary)
                or not 0
                < signal.assistant_turn - boundary.assistant_turn
                <= request.window_turns
                or not signal.in_force
                or not _is_incident_kind(signal.kind)
                or (trip is not None and _order(boundary) >= _order(trip))
            ):
                continue
            manifest = manifests.get(
                (boundary.package_id, boundary.installed_messages_hash)
            )
            if manifest is None:
                continue
            rendered = manifest.rendered_stamps.get(signal.item_key)
            if rendered == signal.expected_stamp:
                continue
            if (
                rendered is not None
                and signal.item_key not in manifest.contradicted_item_keys
            ):
                continue
            incident_key = _incident_key(request.experiment_id, signal)
            if incident_key in keys:
                continue
            keys.add(incident_key)
            incidents.append(
                ModelAttributedIncident(
                    dedup_key=incident_key,
                    event_id=signal.event_id,
                    event_time=signal.event_time,
                    stamp_evidence="ABSENT" if rendered is None else "CONTRADICTED",
                    manifest_source_ref=manifest.source_ref,
                )
            )
            if trip is None and _kill_switch_reached(keys):
                trip = signal

        evidence: list[ModelBoundaryPackageEvidence] = []
        for boundary in sorted(boundaries.values(), key=_order):
            session = first.get(boundary.session_id)
            if (
                session is None
                or base_arms.get(boundary.session_id) != "on"
                or _order(boundary) < _order(session)
                or (trip is not None and _order(boundary) >= _order(trip))
            ):
                continue
            manifest = manifests.get(
                (boundary.package_id, boundary.installed_messages_hash)
            )
            evidence.append(
                ModelBoundaryPackageEvidence(
                    session_id=boundary.session_id,
                    boundary_uuid=boundary.boundary_uuid,
                    installed_messages_hash=boundary.installed_messages_hash,
                    status="AVAILABLE" if manifest is not None else "UNAVAILABLE",
                    manifest_source_ref=manifest.source_ref
                    if manifest is not None
                    else None,
                )
            )
        unreadable = sum(row.status == "UNAVAILABLE" for row in evidence)
        hold = unreadable * 10 > len(evidence)
        ledger_rows: tuple[str, ...] = ()
        ledger_request_id = None
        if trip is not None:
            ledger_request_id = uuid5(
                NAMESPACE_URL,
                json.dumps(
                    ["compaction-kill", request.experiment_id], separators=(",", ":")
                ),
            )
            stamp = (
                trip.event_time.astimezone(UTC)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            )
            ledger_rows = (
                f"{stamp} | FRICTION | lane={_safe_cell(request.ledger_lane)} "
                f"| existing={_safe_cell(request.ledger_ticket)} "
                f"| experiment_id={_safe_cell(request.experiment_id)} "
                f"| id=compaction-kill:{_safe_cell(request.experiment_id)} "
                f"| req={ledger_request_id} "
                f"| {_safe_cell(request.friction_cost_estimate)} "
                "| two distinct mod-attributed incidents; compaction arm=off",
            )
        return ModelArmAssignmentResult(
            correlation_id=request.correlation_id,
            experiment_id=request.experiment_id,
            assignments=tuple(
                ModelSessionArm(
                    session_id=event.session_id,
                    first_event_id=event.event_id,
                    block_index=i // 2,
                    assigned_arm=base_arms[event.session_id],
                    arm="off"
                    if trip is not None or hold
                    else base_arms[event.session_id],
                )
                for i, event in enumerate(sessions)
            ),
            markers=tuple(
                ModelExperimentMarker(
                    event_id=event.event_id,
                    session_id=event.session_id,
                    experiment_id=request.experiment_id,
                    arm="off"
                    if trip is not None and _order(event) >= _order(trip)
                    else base_arms[event.session_id],
                )
                for event in sessions
            ),
            incidents=tuple(incidents),
            boundary_evidence=tuple(evidence),
            kill_switch=trip is not None,
            disabled_at_event_id=trip.event_id if trip is not None else None,
            ledger_request_id=ledger_request_id,
            ledger_rows=ledger_rows,
            on_boundaries=len(evidence),
            unreadable_on_boundaries=unreadable,
            status="HOLD" if hold else "READY",
            hold_reasons=("NO_PACKAGE_EVIDENCE",) if hold else (),
        )
