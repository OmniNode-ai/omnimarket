# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Drop a work-ledger query whose caller has left, and publish no answer (OMN-26433).

The query node is the one consumer of its command topic. A request whose caller stopped waiting was
answered like a live one, and with one partition the dead requests were worked one by one while live
callers waited (consumer lag 4253 on 2026-11-23). This handler is the contract's declared handler:
it ages a request by its ``requested_at`` against its own ``max_age_seconds`` (else the contract's
``work_ledger_query.request_expiry.default_max_age_seconds``) and returns ``None`` for an expired one,
which the runtime's dispatch callback turns into no published answer. Every other request is answered
by ``HandlerWorkLedgerQuery``, unchanged. A request with no ``requested_at`` cannot be aged and is
answered, so a client that does not send one behaves as before.

In-process readers (the reader node's point-read adapter) build ``HandlerWorkLedgerQuery`` directly
and never go through this handler: nobody left waiting on a call in the same process.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.models.work_ledger_query import (
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerQueryResult,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    ProtocolWorkLedgerQueryReader,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_rows_window_cache import (
    RowsWindowCache,
)

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"
_LOGGER = logging.getLogger("omnimarket.nodes.node_work_ledger_query_effect")


def load_request_expiry_default(contract_path: Path) -> float:
    """The contract's ``work_ledger_query.request_expiry.default_max_age_seconds`` (positive)."""
    with contract_path.open(encoding="utf-8") as handle:
        contract = yaml.safe_load(handle)
    section = (contract.get("work_ledger_query") or {}).get("request_expiry") or {}
    value = section.get("default_max_age_seconds")
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ValueError(
            "work_ledger_query.request_expiry.default_max_age_seconds must be positive"
        )
    return float(value)


class HandlerWorkLedgerQueryExpiry:
    """The query node's runtime handler: expired requests get no answer."""

    def __init__(
        self,
        *,
        contract_path: Path | None = None,
        now: Callable[[], datetime] | None = None,
        reader: ProtocolWorkLedgerQueryReader | None = None,
        rows_window_cache: RowsWindowCache | None = None,
    ) -> None:
        path = contract_path or _CONTRACT_PATH
        self._default_max_age_s = load_request_expiry_default(path)
        self._now = now if now is not None else lambda: datetime.now(UTC)
        self._inner = HandlerWorkLedgerQuery(
            contract_path=path,
            now=self._now,
            reader=reader,
            rows_window_cache=rows_window_cache,
        )
        self.terminal_event = self._inner.terminal_event
        self.expired_dropped = 0

    def handle(
        self, request: ModelWorkLedgerQueryRequest
    ) -> ModelWorkLedgerQueryResult | None:
        """Answer the request, or return None (no answer) when its caller's bound has passed."""
        if not isinstance(request, ModelWorkLedgerQueryRequest):
            raise TypeError("work ledger query handler requires its contract payload")
        if request.requested_at is not None:
            max_age_s = (
                request.max_age_seconds
                if request.max_age_seconds is not None
                else self._default_max_age_s
            )
            age_s = (self._now() - request.requested_at).total_seconds()
            if age_s > max_age_s:
                self.expired_dropped += 1
                _LOGGER.warning(
                    "work ledger query request expired, no answer published: "
                    "correlation_id=%s query=%s age_s=%d max_age_s=%d expired_dropped=%d",
                    request.correlation_id,
                    request.query.value,
                    round(age_s),
                    round(max_age_s),
                    self.expired_dropped,
                )
                return None
        return self._inner.handle(request)
