# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Effect handler of the build-to-lifecycle bridge: the CI read and the evidence writes.

The CI read is the GitHub gateway's typed ``ci_checks`` request, one per PR, answered by the same
effect handler the ``onex scm`` reads use; nothing here shells out to ``gh`` or reads a token. The
evidence write puts JSON files under ``<state root>/e2e-runs/<correlation_id>/``, the state root being
the runtime's declared one (:func:`resolve_onex_state_root`). The one model each request returns is
published by the runtime to the topic ``published_events`` maps its class name to, among the contract's
``event_bus.publish_topics``.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

from omnimarket.config.state_root import resolve_onex_state_root
from omnimarket.handlers.scm.handler_gateway_ci_reader import HandlerGatewayCiReader
from omnimarket.models.e2e_bridge import (
    ModelE2eCiReadCompleted,
    ModelE2eCiReadRequest,
    ModelE2eEvidenceWriteRequest,
    ModelE2eEvidenceWritten,
    ModelE2ePrCiReading,
)

_UNPARSEABLE = "invalid pr_ref format"


class ProtocolCiChecksReader(Protocol):
    """Reads one PR's required-check state into the shared bridge model."""

    async def read_ci(
        self, *, pr_ref: str, repo: str, pr_number: int, correlation_id: UUID
    ) -> ModelE2ePrCiReading: ...


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


class HandlerE2eBridgeEffect:
    """EFFECT: read CI for the PRs of a build, or write a run's evidence files."""

    def __init__(
        self,
        reader: ProtocolCiChecksReader | None = None,
        *,
        state_root: Path | None = None,
        now: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._reader = reader
        self._state_root = state_root
        self._now = now
        self._sleep = sleep

    async def handle(
        self, request: ModelE2eCiReadRequest | ModelE2eEvidenceWriteRequest
    ) -> ModelE2eCiReadCompleted | ModelE2eEvidenceWritten:
        if isinstance(request, ModelE2eCiReadRequest):
            return await self._read_ci(request)
        if isinstance(request, ModelE2eEvidenceWriteRequest):
            return self._write_evidence(request)
        raise TypeError(f"unsupported e2e bridge request {type(request).__name__}")

    async def _read_ci(self, request: ModelE2eCiReadRequest) -> ModelE2eCiReadCompleted:
        if request.not_before is not None:
            wait = (request.not_before - self._now()).total_seconds()
            if wait > 0:
                await self._sleep(wait)
        reader = self._reader or HandlerGatewayCiReader()
        readings = [
            await self._read_one(reader, request, pr_ref) for pr_ref in request.pr_refs
        ]
        return ModelE2eCiReadCompleted(
            correlation_id=request.correlation_id,
            pr_refs=request.pr_refs,
            attempt=request.attempt,
            deadline_at=request.deadline_at,
            timeout_seconds=request.timeout_seconds,
            interval_seconds=request.interval_seconds,
            dry_run=request.dry_run,
            read_at=self._now(),
            readings=tuple(readings),
        )

    async def _read_one(
        self,
        reader: ProtocolCiChecksReader,
        request: ModelE2eCiReadRequest,
        pr_ref: str,
    ) -> ModelE2ePrCiReading:
        parsed = _parse_pr_ref(pr_ref)
        if parsed is None:
            return ModelE2ePrCiReading(
                pr_ref=pr_ref, overall="unparseable", error=_UNPARSEABLE
            )
        try:
            repo, pr_number = parsed
            return await reader.read_ci(
                pr_ref=pr_ref,
                repo=repo,
                pr_number=pr_number,
                correlation_id=request.correlation_id,
            )
        except Exception as exc:
            return ModelE2ePrCiReading(
                pr_ref=pr_ref, overall="unreadable", error=str(exc)[:200]
            )

    def _write_evidence(
        self, request: ModelE2eEvidenceWriteRequest
    ) -> ModelE2eEvidenceWritten:
        root = self._state_root or resolve_onex_state_root(
            purpose="the e2e bridge evidence bundle"
        )
        run_dir = root / "e2e-runs" / str(request.correlation_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        written_at = self._now()
        for evidence in request.files:
            body = {**evidence.data, "written_at": written_at.isoformat()}
            target = run_dir / evidence.filename
            staging = run_dir / f".{evidence.filename}.tmp"
            staging.write_text(json.dumps(body, indent=2, default=str), "utf-8")
            staging.replace(target)
        return ModelE2eEvidenceWritten(
            correlation_id=request.correlation_id,
            directory=str(run_dir),
            filenames=tuple(f.filename for f in request.files),
            written_at=written_at,
        )


def _parse_pr_ref(pr_ref: str) -> tuple[str, int] | None:
    """The repo and PR number of ``owner/repo#number``, or None for an invalid ref."""
    parts = pr_ref.split("#")
    if len(parts) != 2:
        return None
    repo, number = parts[0].strip(), parts[1].strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        return None
    if not number.isdigit():
        return None
    try:
        pr_number = int(number)
    except ValueError:
        return None
    return (repo, pr_number) if pr_number >= 1 else None


__all__: list[str] = [
    "HandlerE2eBridgeEffect",
    "ProtocolCiChecksReader",
]
