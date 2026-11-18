# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reads the ledger of record through the bus read command (``onex ledger query --stdin``) (OMN-25537)."""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import ValidationError

from omnimarket.nodes.node_work_ledger_host_mirror_effect.protocols import (
    LedgerReadError,
    ModelLedgerRow,
    ModelLedgerRowsPage,
)

_STAMP = "%Y-%m-%dT%H:%M:%SZ"


class AdapterBusRead:
    """One typed rows query per call. A nonzero exit, an unreadable answer, an answer that is not
    ``ok`` or one that carries an error or a refusal is a LedgerReadError, never an empty page."""

    def __init__(
        self,
        command: Sequence[str],
        timeout_seconds: float,
        kinds: Sequence[str] = (),
    ) -> None:
        if not command:
            raise LedgerReadError("the ledger read command is empty")
        self._command = list(command)
        self._timeout = timeout_seconds
        self._kinds = list(kinds)

    def read_rows(
        self, *, since: datetime, cursor: str | None, page_size: int
    ) -> ModelLedgerRowsPage:
        request: dict[str, object] = {
            "correlation_id": str(uuid.uuid4()),
            "query": "rows",
            "since": since.astimezone(UTC).strftime(_STAMP),
            "page_size": page_size,
        }
        if self._kinds:
            request["kinds"] = self._kinds
        if cursor is not None:
            request["cursor"] = cursor
        try:
            done = subprocess.run(
                self._command,
                input=json.dumps(request),
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LedgerReadError(
                f"the ledger read command did not run: {exc}"
            ) from exc
        if done.returncode != 0:
            raise LedgerReadError(
                f"the ledger read command exited {done.returncode}: {done.stderr.strip()[-300:]}"
            )
        return self._page(done.stdout)

    @staticmethod
    def _page(text: str) -> ModelLedgerRowsPage:
        try:
            answer = json.loads(text)
        except ValueError as exc:
            raise LedgerReadError(f"the ledger read answer is not JSON: {exc}") from exc
        if not isinstance(answer, dict):
            raise LedgerReadError("the ledger read answer is not an object")
        if answer.get("status") != "ok" or answer.get("error") or answer.get("refusal"):
            raise LedgerReadError(
                f"the ledger read answered status={answer.get('status')!r} "
                f"error={answer.get('error')!r} refusal={answer.get('refusal')!r}"
            )
        try:
            rows = tuple(
                ModelLedgerRow(
                    row_id=str(row["row_id"]),
                    row_ts=row["row_ts"],
                    raw_row=str(row["raw_row"]),
                )
                for row in answer["rows"]
            )
            freshness = answer.get("freshness") or {}
            newest = freshness.get("newest_row_ts")
            return ModelLedgerRowsPage(
                rows=rows,
                next_cursor=answer.get("next_cursor"),
                record_newest_row_ts=newest,
            )
        except (KeyError, TypeError, ValidationError) as exc:
            raise LedgerReadError(
                f"the ledger read answer is malformed: {exc}"
            ) from exc
