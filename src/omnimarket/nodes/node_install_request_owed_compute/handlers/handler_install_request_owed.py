# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerInstallRequestOwed: the installs still owed and the line each one owns.

Pure definition-B compute: ``handle(request: ModelInstallOwedInput) -> ModelInstallOwedReading``.
No clock (``now`` is the request's time), no I/O. The same input gives the same reading.

A request with no verified result is owed; one that has is cleared, whatever order the
records arrived in. A request repeated under one id counts once (the first kept). The owed
requests run in the order of ``requested_at`` then the request id. Inside its expiry an owed
request owns its ``NOT RUNNING`` line; past its expiry the line is the one the throughput
tick shows, marked expired. A non-verified result keeps the request owed and is named in its
line, the newest result by ``at``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from omnimarket.models.install_request import (
    EXPIRED_MARK,
    NOT_RUNNING_MARK,
    EnumInstallRequestStatus,
    ModelInstallOwedInput,
    ModelInstallOwedLine,
    ModelInstallOwedReading,
    ModelInstallRequest,
    ModelInstallRequestResult,
)


def _utc_stamp(moment: datetime) -> str:
    """The stamp as UTC with a ``Z``: the 2026-11-15T01:00:00Z form the tick matches on."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _latest_result(
    results: tuple[ModelInstallRequestResult, ...],
) -> ModelInstallRequestResult | None:
    """The newest result by ``at``; the newest by ``(at, revision)`` when two stand at one time."""
    if not results:
        return None
    return max(results, key=lambda r: (r.at, r.revision))


class HandlerInstallRequestOwed:
    """The owed reading: the count, the expired count and one line per owed request."""

    def handle(self, request: ModelInstallOwedInput) -> ModelInstallOwedReading:
        """Read every request and result; the same records give the same lines in the same order."""
        by_id: dict[UUID, ModelInstallRequest] = {}
        for req in request.requests:
            by_id.setdefault(req.request_id, req)
        cleared = self._cleared_ids(request)
        owed = [req for req in by_id.values() if req.request_id not in cleared]
        owed.sort(key=lambda req: (req.requested_at, req.request_id))
        lines: list[ModelInstallOwedLine] = []
        expired = 0
        for req in owed:
            past_expiry = request.now >= req.expires_at
            suffix = self._last_result_suffix(request.results, req.request_id)
            if past_expiry:
                expired += 1
                lines.append(
                    ModelInstallOwedLine(
                        request_id=req.request_id,
                        kind="expired",
                        line=(
                            f"{EXPIRED_MARK} {req.request_id} {req.unit} on {req.host}: "
                            f"{req.repository}@{req.ref} requested by lane {req.lane} at "
                            f"{_utc_stamp(req.requested_at)}, expired {_utc_stamp(req.expires_at)}"
                            + suffix
                        ),
                    )
                )
            else:
                lines.append(
                    ModelInstallOwedLine(
                        request_id=req.request_id,
                        kind="not_running",
                        line=(
                            f"{req.unit} {NOT_RUNNING_MARK}: owned by install request "
                            f"{req.request_id} ({req.repository}@{req.ref} on {req.host}, "
                            f"lane {req.lane}, expires {_utc_stamp(req.expires_at)})"
                            + suffix
                        ),
                    )
                )
        return ModelInstallOwedReading(
            owed=len(owed),
            expired=expired,
            lines=tuple(lines),
        )

    def _cleared_ids(self, request: ModelInstallOwedInput) -> set[UUID]:
        """The ids with a verified result; the order of the records does not matter."""
        return {
            result.request_id
            for result in request.results
            if result.status is EnumInstallRequestStatus.VERIFIED
        }

    def _last_result_suffix(
        self, results: tuple[ModelInstallRequestResult, ...], request_id: UUID
    ) -> str:
        """``; last result <status>: <reason>`` from the newest result that is not verified."""
        latest = _latest_result(
            tuple(
                result
                for result in results
                if result.request_id == request_id
                and result.status is not EnumInstallRequestStatus.VERIFIED
            )
        )
        if latest is None:
            return ""
        return f"; last result {latest.status.value}: {latest.reason}"


__all__: list[str] = ["HandlerInstallRequestOwed"]
