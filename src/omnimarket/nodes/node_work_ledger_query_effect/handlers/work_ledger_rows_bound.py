# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bounded rows window of node_work_ledger_query_effect (OMN-23541).

A ``rows`` query with no ``since`` used to read the whole ``work_ledger_rows``
table and filter afterwards. The window's start is ``until - window``; the
window's days come from the node's overlay field ``default_rows_window_days``
(positive int), falling back to the contract's
``work_ledger_query.rows_default_window_days``. The number is never in code.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerQueryRequest,
)

NODE_NAME = "node_work_ledger_query_effect"
OVERLAY_FILE_ENV = "ONEX_LEDGER_QUERY_OVERLAY"


class ModelRowsWindowOverlay(BaseModel):
    """The deployment fact of the bounded rows window; no key has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    default_rows_window_days: int = Field(ge=1)


def _contract_window_days(contract_path: Path) -> int:
    """The contract's ``work_ledger_query.rows_default_window_days`` (positive int)."""
    with contract_path.open(encoding="utf-8") as handle:
        contract = yaml.safe_load(handle)
    section = contract.get("work_ledger_query") or {}
    days = section.get("rows_default_window_days")
    if not isinstance(days, int) or isinstance(days, bool) or days < 1:
        raise ValueError(
            "work_ledger_query.rows_default_window_days must be a positive int"
        )
    return days


def load_rows_window(
    contract_path: Path, environ: Mapping[str, str] | None = None
) -> timedelta:
    """The bounded rows window: the overlay's days, else the contract's.

    An invalid overlay raises; it never silently falls back to the contract.
    """
    try:
        overlay = load_structured_overlay(
            NODE_NAME, ModelRowsWindowOverlay, environ, file_env=OVERLAY_FILE_ENV
        )
    except NodeOverlayNotFoundError:
        return timedelta(days=_contract_window_days(contract_path))
    return timedelta(days=overlay.default_rows_window_days)


def bounded_since(
    request: ModelWorkLedgerQueryRequest, until: datetime, window: timedelta
) -> datetime | None:
    """The effective start of a bounded rows query, else None.

    Bounded only when the query is ``rows`` with no ``since``, ``row_ref`` or
    ``history``; every other request is not bounded and behaves as before.
    """
    if (
        request.query is EnumWorkLedgerQueryKind.ROWS
        and request.since is None
        and request.row_ref is None
        and request.history is None
    ):
        return until - window
    return None
