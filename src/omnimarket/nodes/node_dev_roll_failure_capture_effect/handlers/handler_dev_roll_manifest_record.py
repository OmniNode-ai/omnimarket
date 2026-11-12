# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Manifest handler of node_dev_roll_failure_capture_effect (OMN-24013).

Records the dev roll manifest the deploy agent published, as UTF-8 JSON, into
the injected artifact store. Never raises out of ``handle``: a missing or
invalid job id or issuer, an unreadable overlay, or a store that cannot write
all come back as a typed ``recorded=False`` result.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_dev_roll_failure_capture_effect.handlers.store_dev_roll_artifact import (
    store_from_policy,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.models import (
    ModelDevRollCapturePolicy,
    ModelDevRollManifestEvent,
    ModelDevRollManifestRecorded,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.overlay import (
    load_capture_policy,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.protocols import (
    ProtocolDevRollArtifactStore,
)

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class HandlerDevRollManifestRecord:
    """``handle(request) -> ModelDevRollManifestRecorded``.

    The runtime builds the handler with no arguments; the store is then built
    lazily on the first request from the policy (constructor argument, else the
    overlay) and cached on the instance. A test passes a store in.
    """

    def __init__(
        self,
        store: ProtocolDevRollArtifactStore | None = None,
        *,
        policy: ModelDevRollCapturePolicy | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._store = store
        self._policy = policy
        self._environ = environ

    def _resolve_store(self) -> ProtocolDevRollArtifactStore:
        if self._store is None:
            environ = os.environ if self._environ is None else self._environ
            if self._policy is None:
                self._policy = load_capture_policy(environ)
            self._store = store_from_policy(self._policy, environ)
        return self._store

    def handle(
        self, request: ModelDevRollManifestEvent
    ) -> ModelDevRollManifestRecorded:
        job_id = request.job_id
        issuer = request.issuer
        if not job_id or not job_id.strip():
            return ModelDevRollManifestRecorded(
                job_id=job_id,
                issuer=issuer,
                recorded=False,
                artifact_key=None,
                error="job_id is missing or blank",
            )
        if not _JOB_ID_RE.fullmatch(job_id):
            return ModelDevRollManifestRecorded(
                job_id=job_id,
                issuer=issuer,
                recorded=False,
                artifact_key=None,
                error=f"job_id {job_id!r} has characters outside letters, digits, '.', '_' and '-'",
            )
        if not issuer or not issuer.strip():
            return ModelDevRollManifestRecorded(
                job_id=job_id,
                issuer=issuer,
                recorded=False,
                artifact_key=None,
                error="issuer is missing or blank",
            )
        key = f"manifests/{job_id}.json"
        payload = json.dumps(request.model_dump(mode="json"), sort_keys=True).encode(
            "utf-8"
        )
        try:
            store = self._resolve_store()
        except NodeOverlayError as exc:
            return ModelDevRollManifestRecorded(
                job_id=job_id,
                issuer=issuer,
                recorded=False,
                artifact_key=None,
                error=str(exc),
            )
        try:
            store.write(key, payload)
        except (OSError, ValueError) as exc:
            return ModelDevRollManifestRecorded(
                job_id=job_id,
                issuer=issuer,
                recorded=False,
                artifact_key=None,
                error=str(exc),
            )
        return ModelDevRollManifestRecorded(
            job_id=job_id,
            issuer=issuer,
            recorded=True,
            artifact_key=key,
            error=None,
        )
