# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Standing-orders load error-surfacing tests for HandlerSessionOrchestrator.

OMN-8721: ``_load_standing_orders`` previously had a bare
``except Exception: logger.warning(...); return {}`` that silently swallowed
JSON-decode / IO / parse errors, making a genuine load failure
indistinguishable from "no standing orders configured". Same failure class as
OMN-9561 (silent swallow in ``_fetch_linear_active_tickets``).

Contract:
  * Absent standing-orders file -> legitimate empty result: returns {} AND
    emits a structured log (never a silent return).
  * Malformed JSON / IO error -> real failure: propagates structurally as
    ``SessionStandingOrdersError`` (never returns {}).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from omnimarket.nodes.node_session_orchestrator.handlers.handler_session_orchestrator import (
    HandlerSessionOrchestrator,
    SessionStandingOrdersError,
)


@pytest.mark.integration
def test_load_standing_orders_absent_file_returns_empty_and_logs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An absent standing-orders file is the legitimate empty-result case.

    It must return {} (no priority boosts) but must NOT be silent — a
    structured log record citing the path proves the absence was observed
    rather than swallowed.
    """
    handler = HandlerSessionOrchestrator()
    missing = tmp_path / "does_not_exist" / "standing_orders.json"

    with caplog.at_level(
        logging.WARNING,
        logger="omnimarket.nodes.node_session_orchestrator.handlers.handler_session_orchestrator",
    ):
        result = handler._load_standing_orders(str(missing))

    assert result == {}
    assert any(
        "standing orders" in rec.getMessage().lower()
        and str(missing) in rec.getMessage()
        for rec in caplog.records
    ), "absent standing-orders file must emit a structured log citing the path"


@pytest.mark.integration
def test_load_standing_orders_empty_list_returns_empty(tmp_path: Path) -> None:
    """A present file containing an empty JSON list yields no boosts, no error."""
    handler = HandlerSessionOrchestrator()
    orders_file = tmp_path / "standing_orders.json"
    orders_file.write_text("[]", encoding="utf-8")

    assert handler._load_standing_orders(str(orders_file)) == {}


@pytest.mark.integration
def test_load_standing_orders_malformed_json_propagates(tmp_path: Path) -> None:
    """Malformed JSON is a real parse failure — it must propagate, not return {}."""
    handler = HandlerSessionOrchestrator()
    orders_file = tmp_path / "standing_orders.json"
    orders_file.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(SessionStandingOrdersError):
        handler._load_standing_orders(str(orders_file))


@pytest.mark.integration
def test_load_standing_orders_io_error_propagates(tmp_path: Path) -> None:
    """An IO error (path exists but is unreadable as a file) must propagate.

    Pointing at a directory makes ``open()`` raise ``IsADirectoryError`` — a
    genuine IO failure that must surface structurally, never be swallowed to {}.
    """
    handler = HandlerSessionOrchestrator()
    # A directory exists at this path, so os.path.exists() is True but open() fails.
    dir_path = tmp_path / "standing_orders.json"
    dir_path.mkdir()

    with pytest.raises(SessionStandingOrdersError):
        handler._load_standing_orders(str(dir_path))
