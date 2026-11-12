# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler of node_dev_roll_failure_capture_effect (OMN-24013).

Consumes the deploy agent's dev-roll manifest event and records it through the
injected artifact store.
"""

from __future__ import annotations

import json

from omnimarket.nodes.node_dev_roll_failure_capture_effect.models import (
    EnumDevRollCaptureFailure,
    ModelDevRollCaptureFailed,
    ModelDevRollManifest,
    ModelDevRollManifestRecorded,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.protocols import (
    ProtocolRollArtifactStore,
    RollArtifactStoreError,
)


class HandlerDevRollManifestRecord:
    """Write the manifest record for one manifest event."""

    def __init__(self, store: ProtocolRollArtifactStore) -> None:
        self._store = store

    async def handle(
        self, request: ModelDevRollManifest
    ) -> ModelDevRollManifestRecorded | ModelDevRollCaptureFailed:
        job_id = request.job_id
        manifest_json = json.dumps(request.model_dump(mode="json"), sort_keys=True).encode("utf-8")
        name = f"{job_id}/manifest.json"
        try:
            record_ref = self._store.write(name, manifest_json)
        except RollArtifactStoreError as exc:
            return ModelDevRollCaptureFailed(
                job_id=job_id,
                reason=EnumDevRollCaptureFailure.STORE_UNWRITABLE,
                detail=exc.detail,
            )
        return ModelDevRollManifestRecorded(
            job_id=job_id,
            issuer=request.issuer,
            git_ref=request.git_ref,
            scope=request.scope,
            accepted_at=request.accepted_at,
            record_ref=record_ref,
        )
