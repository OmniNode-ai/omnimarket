# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One fence derivation: read the ledger and pull requests, take the list from compute (OMN-20682).

The ledger file and the open pull requests are read here; the fence list comes from
``node_triage_fences_compute``, so the report is the JSON the triage lanes read.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.events.triage_fences import (
    ModelTriageFencesDeriveRequest,
    ModelTriageFencesOpenPull,
)
from omnimarket.nodes.node_triage_fences_compute.handlers.handler_triage_fences_derive import (
    HandlerTriageFencesDerive,
)

from ..logic.triage_fences_receipts import running_lanes
from ..logic.triage_fences_state import PullReader, WatcherStatePulls, read_open_pulls
from ..models import (
    ModelTriageFencesOptions,
    ModelTriageFencesOverlay,
    ModelTriageFencesReport,
    ModelTriageFencesRequest,
    ModelTriageFencesResult,
)

ReceiptReader = Callable[
    [Path, datetime, float], tuple[tuple[str, ...], tuple[str, ...]]
]


class HandlerTriageFences:
    """Run one derivation against the ledger this handler was built for."""

    def __init__(
        self,
        options: ModelTriageFencesOptions,
        overlay: ModelTriageFencesOverlay,
        *,
        pull_reader: PullReader | None = None,
        receipt_reader: ReceiptReader | None = None,
    ) -> None:
        self._options = options
        self._overlay = overlay
        self._pull_reader = pull_reader
        self._receipt_reader = receipt_reader or running_lanes
        self._derive = HandlerTriageFencesDerive()

    def handle(self, request: ModelTriageFencesRequest) -> ModelTriageFencesResult:
        """Return the report, or exit code 2 and the reason when the run cannot start."""
        ledger = self._options.ledger
        if not ledger.is_file():
            # Rendering an unreadable ledger as "nothing is fenced" is the fail-open this exists to
            # eliminate.
            return ModelTriageFencesResult(
                request_id=request.request_id,
                exit_code=2,
                error=f"ledger path is not a readable file: {ledger}",
            )
        now = request.now or datetime.now(UTC)
        stale_after = (
            request.stale_after_hours or self._overlay.stale_after_minutes / 60
        )
        window = request.pr_window_hours or self._overlay.pr_window_hours
        repos = request.repos if request.repos is not None else self._overlay.repos
        text = ledger.read_text(encoding="utf-8")
        errors: list[str] = []
        pulls: tuple[ModelTriageFencesOpenPull, ...] = ()
        if request.include_open_prs:
            reader = self._pull_reader or WatcherStatePulls(
                now=now, path=self._options.watcher_state or self._overlay.watcher_state
            )
            pulls = tuple(
                ModelTriageFencesOpenPull(
                    repo=repo,
                    number=pull.get("number"),
                    title=str(pull.get("title", "")),
                    head_ref_name=str(pull.get("headRefName", "")),
                    updated_at=str(pull.get("updatedAt", "")),
                    url=pull.get("url"),
                )
                for repo, pull in read_open_pulls(
                    repos=list(repos),
                    reader=reader,
                    errors=errors,
                )
            )
        receipt_dir = self._overlay.receipt_dir.expanduser()
        lanes, receipt_errors = self._receipt_reader(
            receipt_dir, now, self._overlay.running_receipt_max_age_minutes
        )
        errors.extend(receipt_errors)
        verdict = self._derive.handle(
            ModelTriageFencesDeriveRequest(
                ledger_name=ledger.name,
                ledger_text=text,
                now=now,
                stale_after_hours=stale_after,
                pr_window_hours=window,
                open_pulls=pulls,
                running_lanes=lanes,
            )
        )
        report = ModelTriageFencesReport(
            now=now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            ledger=str(ledger),
            fenced_tickets=verdict.fenced_tickets,
            open_claims=verdict.open_claims,
            stale_claims=verdict.stale_claims,
            stale_after_hours=stale_after,
            open_pr_fences=verdict.open_pr_fences,
            unparsed_rows=verdict.unparsed_rows,
            errors=tuple(errors),
            counts=verdict.counts,
        )
        return ModelTriageFencesResult(
            request_id=request.request_id, exit_code=0, report=report
        )
