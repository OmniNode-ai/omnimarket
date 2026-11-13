# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Incident handler of node_dev_roll_failure_capture_effect (OMN-24013).

One dev roll incident in, one typed terminal out. The terminal's outcome is
never success-like: an unreadable incident, a missing issuer, an unwritable
artifact store or an unreadable overlay all yield ``unknown`` with a typed
reason. Never raises out of ``handle``. The handler holds no producer: the
runtime publishes the returned terminal on the contract's ``published_events``
topic through its event_bus.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from pydantic import JsonValue

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_dev_roll_failure_capture_effect.handlers.store_dev_roll_artifact import (
    store_from_policy,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.models import (
    EnumDevRollOutcome,
    EnumDevRollUnknownReason,
    ModelDevRollCapturePolicy,
    ModelDevRollIncidentEvent,
    ModelDevRollLogArtifact,
    ModelDevRollTerminal,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.overlay import (
    load_capture_policy,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.protocols import (
    ProtocolDevRollArtifactStore,
)

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _as_int(value: JsonValue) -> int:
    """A validated non-negative int out of a JSON value (``_incident_problem`` has already checked it)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise ValueError(f"{value!r} is not an int")


def _sanitize_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


class HandlerDevRollIncidentCapture:
    """``handle(request) -> ModelDevRollTerminal``.

    The runtime builds the handler with no arguments; the store and policy are
    resolved lazily on the first request and cached. A test passes them in.
    """

    def __init__(
        self,
        store: ProtocolDevRollArtifactStore | None = None,
        *,
        policy: ModelDevRollCapturePolicy | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._policy = policy
        self._environ = environ
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def _resolve(
        self,
    ) -> tuple[ProtocolDevRollArtifactStore, ModelDevRollCapturePolicy]:
        environ = os.environ if self._environ is None else self._environ
        if self._policy is None:
            self._policy = load_capture_policy(environ)
        if self._store is None:
            self._store = store_from_policy(self._policy, environ)
        return self._store, self._policy

    def _terminal(
        self,
        request: ModelDevRollIncidentEvent,
        outcome: EnumDevRollOutcome,
        *,
        reason: EnumDevRollUnknownReason | None = None,
        detail: str | None = None,
        artifacts: tuple[ModelDevRollLogArtifact, ...] = (),
        manifest: tuple[bool, bool | None] = (False, None),
        terminal_key: str | None = None,
    ) -> ModelDevRollTerminal:
        return ModelDevRollTerminal(
            terminal_id=ModelDevRollTerminal.make_terminal_id(request.job_id),
            job_id=request.job_id,
            issuer=request.issuer,
            outcome=outcome,
            unknown_reason=reason,
            detail=detail,
            observed_at=self._clock(),
            incident_at=request.at,
            commit=request.commit,
            error=request.error,
            recovery_verified=request.recovery_verified,
            manifest_seen=manifest[0],
            manifest_issuer_matches=manifest[1],
            health_probe_count=len(request.health_probes),
            log_artifacts=artifacts,
            terminal_artifact_key=terminal_key,
        )

    def handle(self, request: ModelDevRollIncidentEvent) -> ModelDevRollTerminal:
        """One incident in, one typed terminal out; the outcome is never success-like on a doubt."""
        unknown = EnumDevRollOutcome.UNKNOWN
        try:
            store, policy = self._resolve()
        except NodeOverlayError as exc:
            return self._terminal(
                request,
                unknown,
                reason=EnumDevRollUnknownReason.OVERLAY_UNREADABLE,
                detail=str(exc),
            )
        problem = self._incident_problem(request)
        job_id = request.job_id
        if problem is not None or job_id is None:
            return self._terminal(
                request,
                unknown,
                reason=EnumDevRollUnknownReason.INCIDENT_UNREADABLE,
                detail=problem,
            )
        # The log is the evidence this node exists to keep: it is captured even when the issuer is missing.
        manifest = self._manifest_facts(store, job_id, request.issuer)
        artifacts = self._capture_logs(store, policy, job_id, request)
        if not request.issuer or not request.issuer.strip():
            return self._terminal(
                request,
                unknown,
                reason=EnumDevRollUnknownReason.ISSUER_MISSING,
                detail="the incident has no issuer",
                artifacts=artifacts,
                manifest=manifest,
            )
        if any(not artifact.written for artifact in artifacts):
            return self._terminal(
                request,
                unknown,
                reason=EnumDevRollUnknownReason.ARTIFACT_STORE_UNWRITABLE,
                detail="a log artifact could not be written to the store",
                artifacts=artifacts,
                manifest=manifest,
            )
        key = f"terminals/{job_id}.json"
        terminal = self._terminal(
            request,
            EnumDevRollOutcome.RECOVERED
            if request.recovery_verified
            else EnumDevRollOutcome.UNRECOVERED,
            artifacts=artifacts,
            manifest=manifest,
            terminal_key=key,
        )
        try:
            store.write(key, terminal.model_dump_json().encode("utf-8"))
        except (OSError, ValueError) as exc:
            return self._terminal(
                request,
                unknown,
                reason=EnumDevRollUnknownReason.ARTIFACT_STORE_UNWRITABLE,
                detail=f"the terminal could not be written: {exc}",
                artifacts=artifacts,
                manifest=manifest,
            )
        return terminal

    @staticmethod
    def _incident_problem(request: ModelDevRollIncidentEvent) -> str | None:
        job_id = request.job_id
        if not job_id or not job_id.strip():
            return "job_id is missing or blank"
        if not _NAME_RE.fullmatch(job_id):
            return f"job_id {job_id!r} has characters outside letters, digits, '.', '_' and '-'"
        if request.recovery_verified is None:
            return "recovery_verified is unset"
        for index, capture in enumerate(request.log_captures):
            for field in (
                "service",
                "container",
                "bytes_total",
                "bytes_kept",
                "complete",
                "sha256",
                "log",
            ):
                if field not in capture:
                    return f"log_captures[{index}] is missing {field!r}"
            service = capture["service"]
            container = capture["container"]
            if not isinstance(service, str) or not isinstance(container, str):
                return f"log_captures[{index}] service and container must be strings"
            bytes_total = capture["bytes_total"]
            bytes_kept = capture["bytes_kept"]
            if not isinstance(bytes_total, int) or bytes_total < 0:
                return f"log_captures[{index}] bytes_total must be a non-negative int"
            if not isinstance(bytes_kept, int) or bytes_kept < 0:
                return f"log_captures[{index}] bytes_kept must be a non-negative int"
            if not isinstance(capture["complete"], bool):
                return f"log_captures[{index}] complete must be a bool"
            sha256 = capture["sha256"]
            if not isinstance(sha256, str) or len(sha256) != 64:
                return f"log_captures[{index}] sha256 must be a 64-character string"
            if not isinstance(capture["log"], str):
                return f"log_captures[{index}] log must be a string"
        return None

    @staticmethod
    def _manifest_facts(
        store: ProtocolDevRollArtifactStore, job_id: str, issuer: str | None
    ) -> tuple[bool, bool | None]:
        key = f"manifests/{job_id}.json"
        try:
            raw = store.read(key)
        except (OSError, ValueError):
            return False, None
        if raw is None:
            return False, None
        try:
            manifest = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return False, None
        if not isinstance(manifest, dict):
            return False, None
        return True, manifest.get("issuer") == issuer

    @staticmethod
    def _capture_logs(
        store: ProtocolDevRollArtifactStore,
        policy: ModelDevRollCapturePolicy,
        job_id: str,
        request: ModelDevRollIncidentEvent,
    ) -> tuple[ModelDevRollLogArtifact, ...]:
        artifacts: list[ModelDevRollLogArtifact] = []
        for capture in request.log_captures:
            service = _sanitize_name(str(capture["service"]))
            container = _sanitize_name(str(capture["container"]))[:12]
            log_bytes = str(capture["log"]).encode("utf-8")
            truncated = len(log_bytes) > policy.max_log_bytes
            if truncated:
                log_bytes = log_bytes[-policy.max_log_bytes :]
            key = f"incidents/{job_id}/{service}-{container}.log"
            source_complete = bool(capture["complete"])
            try:
                sha256 = store.write(key, log_bytes)
            except (OSError, ValueError) as exc:
                artifacts.append(
                    ModelDevRollLogArtifact(
                        service=str(capture["service"]),
                        container=str(capture["container"]),
                        artifact_key=None,
                        bytes_total=_as_int(capture["bytes_total"]),
                        bytes_kept=_as_int(capture["bytes_kept"]),
                        bytes_written=0,
                        sha256="",
                        source_complete=source_complete,
                        written=False,
                        complete=False,
                        truncated=truncated,
                        error=str(exc),
                    )
                )
                continue
            artifacts.append(
                ModelDevRollLogArtifact(
                    service=str(capture["service"]),
                    container=str(capture["container"]),
                    artifact_key=key,
                    bytes_total=_as_int(capture["bytes_total"]),
                    bytes_kept=_as_int(capture["bytes_kept"]),
                    bytes_written=len(log_bytes),
                    sha256=sha256,
                    source_complete=source_complete,
                    written=True,
                    complete=source_complete and not truncated,
                    truncated=truncated,
                    error=None,
                )
            )
        return tuple(artifacts)
