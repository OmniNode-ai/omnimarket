# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bound the whole candidate enumeration and answer a typed read diagnostic.

The read's former per-request network timeouts did not bound imports, ledger
windows, archive scans or local processing. A host-owned subprocess now shares
one monotonic deadline for all of them. Its process group is reaped on timeout;
no background refresh or grandchild can keep the read's pipes open afterwards.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import BinaryIO

from pydantic import ValidationError

from ..models.model_lab_fill_enumeration import (
    ModelLabFillEnumerationRead,
    ModelLabFillEnumerationRequest,
    ModelLabFillEnumerationResult,
    ModelLabFillEnumerationSourceRead,
    ModelLabFillReaderSnapshot,
)
from .helpers_lab_fill_effect import iso_utc

_COMPLETE = frozenset({"ok", "empty"})
_STDERR_LIMIT = 65536


def _redact(text: str) -> str:
    for name, value in os.environ.items():
        if value and re.search(r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", name, re.I):
            text = text.replace(value, "[redacted]")
    return text


@dataclass(frozen=True)
class _CommandRead:
    returncode: int | None = None
    stdout: bytes = b""
    stderr: str = ""
    error: str = ""


def _stop(process: subprocess.Popen[bytes]) -> None:
    # Every reader is its own session. Clean up its descendants even when the
    # session leader already exited, so a detached child cannot outlive the read.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def _too_large(stdout: BinaryIO, stderr: BinaryIO, limit: int) -> bool:
    return (
        os.fstat(stdout.fileno()).st_size > limit
        or os.fstat(stderr.fileno()).st_size > _STDERR_LIMIT
    )


class HandlerLabFillEnumeration:
    """Read one live or cache-only snapshot before the caller's return margin."""

    def handle(
        self, request: ModelLabFillEnumerationRequest
    ) -> ModelLabFillEnumerationResult:
        started = time.monotonic()
        budget = min(
            request.read_budget_s,
            request.outer_deadline_epoch_s - time.time() - request.return_margin_s,
        )
        deadline = started + max(0.0, budget)
        read = self._read(request, deadline)
        if read.error:
            return self._unread(request, started, read, read.error)
        if read.returncode != 0:
            detail = f"reader-exited:{read.returncode}"
            if not read.stdout.strip():
                detail += "; no-json"
            if not read.stderr:
                detail += "; stderr-empty"
            return self._unread(request, started, read, detail)
        if not read.stdout.strip():
            return self._unread(request, started, read, "reader-printed-no-json")
        try:
            snapshot = ModelLabFillReaderSnapshot.model_validate_json(read.stdout)
        except ValidationError:
            # Do not echo invalid stdout: it may contain credentials from a
            # dependency. Preserve stderr separately after credential redaction.
            return self._unread(request, started, read, "reader-invalid-json-or-schema")
        if time.monotonic() >= deadline:
            return self._unread(request, started, read, "read-deadline-exceeded:decode")
        result = self._result(request, snapshot, started, read)
        if time.monotonic() >= deadline:
            return self._unread(
                request, started, read, "read-deadline-exceeded:normalize"
            )
        return result

    @staticmethod
    def _read(request: ModelLabFillEnumerationRequest, deadline: float) -> _CommandRead:
        if deadline <= time.monotonic():
            return _CommandRead(error="read-deadline-exceeded:before-start")
        try:
            # Regular files avoid pipe-buffer deadlocks, unbounded communicate()
            # allocations and inherited pipes delaying the terminal response.
            with (
                tempfile.TemporaryFile() as stdin,
                tempfile.TemporaryFile() as stdout,
                tempfile.TemporaryFile() as stderr,
            ):
                stdin.write(request.reader_stdin.encode("utf-8"))
                stdin.seek(0)
                if deadline <= time.monotonic():
                    return _CommandRead(error="read-deadline-exceeded:before-start")
                process = subprocess.Popen(
                    request.reader_argv,
                    stdin=stdin,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
                error = ""
                try:
                    while True:
                        if _too_large(stdout, stderr, request.max_output_bytes):
                            error = "reader-output-limit-exceeded"
                            break
                        left = deadline - time.monotonic()
                        if left <= 0:
                            error = "read-deadline-exceeded:reader"
                            break
                        try:
                            process.wait(timeout=min(left, 0.05))
                            break
                        except subprocess.TimeoutExpired:
                            continue
                finally:
                    _stop(process)
                if _too_large(stdout, stderr, request.max_output_bytes):
                    error = "reader-output-limit-exceeded"
                stdout.seek(0)
                stderr.seek(0)
                return _CommandRead(
                    returncode=process.returncode,
                    stdout=stdout.read(request.max_output_bytes),
                    stderr=_redact(
                        stderr.read(_STDERR_LIMIT).decode("utf-8", errors="replace")
                    ),
                    error=error,
                )
        except OSError as exc:
            # An absent executable is a read failure, with a terminal response.
            return _CommandRead(error=f"reader-start-failed:{type(exc).__name__}")

    @staticmethod
    def _unread(
        request: ModelLabFillEnumerationRequest,
        started: float,
        read: _CommandRead,
        error: str,
    ) -> ModelLabFillEnumerationResult:
        return ModelLabFillEnumerationResult(
            source=request.source,
            outcome="UNREAD",
            candidates=(),
            fenced_tickets=(),
            source_diagnostics={
                kind: ModelLabFillEnumerationSourceRead(read="unreadable:" + error)
                for kind in request.kinds
            },
            enumeration_read=ModelLabFillEnumerationRead(
                outcome="failed",
                empty_is_evidence=False,
                failed_reads=tuple(
                    f"{kind}:unreadable:{error}" for kind in request.kinds
                ),
                detail=error,
            ),
            truncated=True,
            read_error=error,
            reader_returncode=read.returncode,
            reader_stderr=read.stderr,
            elapsed_s=time.monotonic() - started,
            finished_utc=iso_utc(time.time()),
        )

    def _result(
        self,
        request: ModelLabFillEnumerationRequest,
        snapshot: ModelLabFillReaderSnapshot,
        started: float,
        read: _CommandRead,
    ) -> ModelLabFillEnumerationResult:
        declared = snapshot.enumeration_read
        if declared is not None and declared.outcome == "failed":
            return self._unread(request, started, read, "reader-declared-failed")
        diagnostics = dict(snapshot.source_diagnostics)
        for kind in request.kinds:
            diagnostics.setdefault(
                kind,
                ModelLabFillEnumerationSourceRead(read="unreadable:missing-diagnostic"),
            )
        for candidate in snapshot.candidates:
            if candidate.get("kind") not in request.kinds:
                return self._unread(
                    request, started, read, "reader-unreported-candidate-kind"
                )
            if diagnostics[str(candidate["kind"])].read == "empty":
                return self._unread(
                    request, started, read, "reader-empty-kind-has-candidates"
                )
        if snapshot.cache_rejected or snapshot.source in ("none", "cache-stale"):
            return self._unread(
                request,
                started,
                read,
                "reader-cache-rejected"
                if snapshot.cache_rejected
                else "reader-source-unreadable",
            )
        failed = tuple(
            f"{kind}:{diagnostics[kind].read}"
            for kind in request.kinds
            if diagnostics[kind].read not in _COMPLETE
        )
        candidates = tuple(
            candidate
            for candidate in snapshot.candidates
            if diagnostics[str(candidate["kind"])].read in _COMPLETE
        )
        for kind in request.kinds:
            if diagnostics[kind].read not in _COMPLETE:
                diagnostics[kind] = diagnostics[kind].model_copy(update={"eligible": 0})
        all_failed = len(failed) == len(request.kinds)
        declared_partial = declared is not None and (
            declared.outcome == "partial" or not declared.empty_is_evidence
        )
        partial = bool(
            failed
            or snapshot.truncated
            or snapshot.source.startswith("cache")
            or declared_partial
        )
        outcome = "UNREAD" if all_failed else "PARTIAL" if partial else "READ"
        detail = (
            "every-kind-unreadable"
            if all_failed
            else "kind-not-read"
            if failed
            else "truncated"
            if snapshot.truncated
            else "served-from-cache"
            if snapshot.source.startswith("cache")
            else ""
        )
        payload = snapshot.model_dump()
        payload.update(
            outcome=outcome,
            candidates=() if all_failed else candidates,
            source_diagnostics=diagnostics,
            enumeration_read=ModelLabFillEnumerationRead(
                outcome="failed"
                if all_failed
                else "partial"
                if partial
                else "complete",
                empty_is_evidence=not partial,
                failed_reads=tuple(
                    dict.fromkeys(
                        (*failed, *(declared.failed_reads if declared else ()))
                    )
                ),
                detail=detail
                or (declared.detail if declared_partial and declared else ""),
            ),
            truncated=partial,
            read_error=detail,
            reader_returncode=read.returncode,
            reader_stderr=read.stderr,
            elapsed_s=time.monotonic() - started,
            finished_utc=iso_utc(time.time()),
        )
        return ModelLabFillEnumerationResult.model_validate(payload)
