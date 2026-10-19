# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read each lane's own receipt into one table row, without I/O (OMN-17427).

A lab-fill report that says "3 of 6 placed" hides why the other three were not. The runner
writes the reason into the lane's receipt (`reason` for a lane no host took, `error` for a setup
refusal, `failure.reason` and `result` for a limit), so the row quotes that text and classifies it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

from ..models import (
    EnumLaneState,
    EnumRefusalClass,
    ModelAdmittingHost,
    ModelLaneReceiptInput,
    ModelLaneReceiptTableRequest,
    ModelLaneReceiptTableResult,
    ModelLaneRetry,
    ModelLaneRow,
)

COLUMNS = ("lane", "ticket", "model", "host", "state", "class", "reason")
RETRY_SUFFIX = "-retry"

# Statuses the runner writes when it ends a lane before an engine ran, with the host chosen or not.
# no-host and the limited-host statuses have their own state; these are the refusals.
_POLICY_STATUSES = frozenset(
    {
        "usage",
        "mint-refused",
        "timeout-refused",
        "prerequisite-unavailable",
        "capability-unavailable",
        "brief-changed",
        "hook-parse-refused",
        "ledger-refused",
        "plugins-unresolved",
    }
)
_OTHER_STATUSES = frozenset(
    {
        "node-unavailable",
        "omni-home-unresolved",
        "repo-unresolved",
        "pr-head-unreadable",
        "pr-head-fetch-failed",
        "ledger-snapshot-failed",
        "glm-key-missing",
    }
)
_LIMITED_STATUSES = frozenset({"host-limited", "host-auth-expired"})
_REFUSED_STATUSES = _POLICY_STATUSES | _OTHER_STATUSES


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def receipt_reason(receipt: Mapping[str, Any]) -> str:
    """The receipt's own words for why a lane ended, verbatim: reason, error, failure, result."""
    for key in ("reason", "error"):
        text = _text(receipt.get(key))
        if text:
            return text
    failure = receipt.get("failure")
    if isinstance(failure, Mapping):
        text = _text(failure.get("reason")) or _text(failure.get("kind"))
    else:
        text = _text(failure)
    return text or _text(receipt.get("result"))


def classify_refusal(
    status: str, reason: str, failure_kind: str = ""
) -> EnumRefusalClass:
    """capacity, policy, limit or other, from the status the runner wrote and its reason text."""
    if status == "no-host":
        # A no-host reason also lists the limited hosts it skipped; the refusal is still capacity.
        return (
            EnumRefusalClass.POLICY
            if "PRIORITY-HOLDBACK" in reason
            else EnumRefusalClass.CAPACITY
        )
    if (
        status in _LIMITED_STATUSES
        or failure_kind == "usage-limit"
        or "usage limit" in reason.lower()
    ):
        return EnumRefusalClass.LIMIT
    if status in _POLICY_STATUSES or "REFUSED" in reason:
        return EnumRefusalClass.POLICY
    return EnumRefusalClass.OTHER


def _row(item: ModelLaneReceiptInput) -> ModelLaneRow:
    def make(
        host: str, state: EnumLaneState, klass: EnumRefusalClass, reason: str
    ) -> ModelLaneRow:
        return ModelLaneRow(
            item.lane, item.ticket, item.model, host or "none", state, klass, reason
        )

    receipt = item.receipt
    if not item.detached:
        return make(
            item.planned_host,
            EnumLaneState.NEVER_STARTED,
            EnumRefusalClass.NONE,
            item.detail
            or (
                f"skipped:{item.skipped}"
                if item.skipped
                else "the dispatch did not start this lane"
            ),
        )
    if receipt is None:
        return make(
            item.planned_host,
            EnumLaneState.NEVER_STARTED,
            EnumRefusalClass.NONE,
            "no receipt was read for this lane",
        )
    status = _text(receipt.get("status"))
    host = _text(receipt.get("host"))
    final = receipt.get("final") is True
    reason = receipt_reason(receipt)
    failure = receipt.get("failure")
    kind = _text(failure.get("kind")) if isinstance(failure, Mapping) else ""
    if status in ("no-host", "node-unavailable") and not host:
        klass = classify_refusal(status, reason)
        state = EnumLaneState.NO_HOST
        if status == "node-unavailable":
            klass = EnumRefusalClass.OTHER
        return make(
            "none",
            state,
            klass,
            reason or f"(the receipt names no reason; status={status})",
        )
    if status in _LIMITED_STATUSES:
        return make(
            host,
            EnumLaneState.HOST_LIMITED,
            EnumRefusalClass.LIMIT,
            reason or f"(the receipt names no reason; status={status})",
        )
    if status in _REFUSED_STATUSES or (final and not host and status != "done"):
        klass = classify_refusal(status, reason, kind)
        return make(
            host,
            EnumLaneState.REFUSED,
            klass,
            reason or f"(the receipt names no reason; status={status or 'unknown'})",
        )
    if not host or status in ("preparing", "placing", ""):
        return make(host, EnumLaneState.PREPARING, EnumRefusalClass.NONE, "")
    return make(host, EnumLaneState.PLACED, EnumRefusalClass.NONE, "")


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " / ").strip()


def render_table(rows: tuple[ModelLaneRow, ...]) -> str:
    """The markdown table: a row per lane, the reason quoted, `-` for a cell with nothing to say."""
    lines = [
        "| " + " | ".join(COLUMNS) + " |",
        "| " + " | ".join("--" for _ in COLUMNS) + " |",
    ]
    for row in rows:
        klass = (
            "-"
            if row.refusal_class is EnumRefusalClass.NONE
            else row.refusal_class.value
        )
        cells = (
            _cell(row.lane),
            _cell(row.ticket or "-"),
            _cell(row.model or "-"),
            _cell(row.host),
            row.state.value,
            klass,
            f'"{_cell(row.reason)}"' if row.reason else "-",
        )
        lines.append("| " + " | ".join(cells) + " |")
    if not rows:
        lines.append("| (no lanes were dispatched) | | | | | | |")
    return "\n".join(lines)


def _login(model: str) -> str:
    """The engine login a lane's model needs on its host: codex, else the Claude login."""
    return "codex" if model == "codex" else "claude"


def _plan_retries(
    items: tuple[ModelLaneReceiptInput, ...],
    rows: tuple[ModelLaneRow, ...],
    hosts: tuple[ModelAdmittingHost, ...],
) -> tuple[ModelLaneRetry, ...]:
    """One retry for each unpinned capacity refusal, on another host that admits its engine.

    Each host's free lanes are spent as retries are planned, so two refusals never share one
    slot. A retry lane, a pinned lane (its host is part of the work) and any refusal that is
    not capacity is never retried."""
    left = {h.name: h.free_lanes for h in hosts}
    retries: list[ModelLaneRetry] = []
    for item, row in zip(items, rows, strict=True):
        if row.refusal_class is not EnumRefusalClass.CAPACITY or row.state not in (
            EnumLaneState.NO_HOST,
            EnumLaneState.REFUSED,
        ):
            continue
        if item.pinned or item.lane.endswith(RETRY_SUFFIX):
            continue
        fit = [
            h
            for h in hosts
            if h.name != item.planned_host
            and left[h.name] > 0
            and _login(item.model) in h.engines
        ]
        if not fit:
            continue
        pick = max(fit, key=lambda h: (left[h.name], -hosts.index(h)))
        left[pick.name] -= 1
        retries.append(
            ModelLaneRetry(item.lane, item.model, item.planned_host, pick.name)
        )
    return tuple(retries)


class HandlerLabFillReceiptTable:
    """Turn the lanes of a run and their receipts into the report table and the capacity retries."""

    def handle(
        self, request: ModelLaneReceiptTableRequest
    ) -> ModelLaneReceiptTableResult:
        """One row per lane, read from its own receipt."""
        rows = tuple(_row(item) for item in request.lanes)
        counts = Counter(row.state.value for row in rows)
        return ModelLaneReceiptTableResult(
            rows=rows,
            table=render_table(rows),
            counts=tuple(counts.items()),
            retries=_plan_retries(request.lanes, rows, request.admitting_hosts),
        )
