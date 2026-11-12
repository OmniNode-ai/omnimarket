# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler of node_dev_roll_failure_capture_effect (OMN-24013).

Consumes the deploy agent's dev-roll incident event and writes the roll's typed
terminal record, with the failed container's captured log, through the
injected artifact store.
"""

from __future__ import annotations

import json

from omnimarket.nodes.node_dev_roll_failure_capture_effect.models import (
    EnumDevRollCaptureFailure,
    EnumDevRollOutcome,
    ModelDevRollCaptureFailed,
    ModelDevRollIncident,
    ModelDevRollLogArtifact,
    ModelDevRollTerminalRecorded,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.overlay import (
    ModelDevRollCaptureSettings,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.protocols import (
    ProtocolRollArtifactStore,
    RollArtifactStoreError,
)


class HandlerDevRollIncidentCapture:
    """Write the terminal record and captured logs for one incident."""

    def __init__(
        self,
        store: ProtocolRollArtifactStore,
        settings: ModelDevRollCaptureSettings,
    ) -> None:
        self._store = store
        self._settings = settings

    async def handle(
        self, request: ModelDevRollIncident
    ) -> ModelDevRollTerminalRecorded | ModelDevRollCaptureFailed:
        job_id = request.job_id
        artifacts: list[ModelDevRollLogArtifact] = []
        store_failed = False

        for capture in request.log_captures:
            log_bytes = capture.log.encode("utf-8")
            complete = capture.complete
            if len(log_bytes) > self._settings.log_byte_cap:
                log_bytes = log_bytes[-self._settings.log_byte_cap:]
                complete = False
            name = f"{job_id}/{capture.service}.log"
            try:
                store_ref = self._store.write(name, log_bytes)
                store_error: str | None = None
            except RollArtifactStoreError as exc:
                store_ref = ""
                store_error = exc.detail
                store_failed = True
            artifacts.append(
                ModelDevRollLogArtifact(
                    service=capture.service,
                    container=capture.container,
                    store_ref=store_ref,
                    bytes_total=capture.bytes_total,
                    bytes_kept=len(log_bytes),
                    complete=complete,
                    redacted=capture.redacted,
                    sha256=capture.sha256,
                    agent_artifact_path=capture.artifact_path,
                    store_error=store_error,
                )
            )

        outcome = self._outcome(request)
        capture_complete = (
            all(a.complete and not a.redacted and a.store_error is None for a in artifacts)
            and len(artifacts) > 0
            and (len(request.health_probes) > 0)
        )

        terminal = ModelDevRollTerminalRecorded(
            job_id=job_id,
            issuer=request.issuer,
            commit=request.commit,
            outcome=outcome,
            restored_revision=request.restored_revision,
            error=request.error,
            recovered_at=request.at,
            capture_complete=capture_complete,
            log_artifacts=tuple(artifacts),
            health_probe_services=tuple(
                probe.get("service", "") for probe in request.health_probes
            ),
            record_ref="",
        )

        terminal_json = json.dumps(terminal.model_dump(mode="json"), sort_keys=True).encode("utf-8")
        terminal_name = f"{job_id}/terminal.json"
        try:
            record_ref = self._store.write(terminal_name, terminal_json)
        except RollArtifactStoreError as exc:
            return ModelDevRollCaptureFailed(
                job_id=job_id,
                reason=EnumDevRollCaptureFailure.STORE_UNWRITABLE,
                detail=exc.detail,
            )

        terminal = ModelDevRollTerminalRecorded(
            job_id=job_id,
            issuer=request.issuer,
            commit=request.commit,
            outcome=outcome,
            restored_revision=request.restored_revision,
            error=request.error,
            recovered_at=request.at,
            capture_complete=capture_complete,
            log_artifacts=tuple(artifacts),
            health_probe_services=terminal.health_probe_services,
            record_ref=record_ref,
        )
        return terminal

    @staticmethod
    def _outcome(request: ModelDevRollIncident) -> EnumDevRollOutcome:
        if request.recovery_error is not None:
            return EnumDevRollOutcome.RECOVERY_FAILED
        if request.recovery_verified:
            return EnumDevRollOutcome.RESTORED_VERIFIED
        return EnumDevRollOutcome.RESTORED_UNVERIFIED
