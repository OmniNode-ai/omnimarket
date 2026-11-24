# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The generated view's banner names the L0 record in words, never by the ledger file's path."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_projection_work_events.ledger_view import (
    parse_ledger_view,
    render_ledger_view,
)

pytestmark = pytest.mark.unit


def test_the_banner_does_not_name_the_ledger_file_path() -> None:
    view = render_ledger_view([])
    assert "docs/tracking" not in view
    assert "rolling work ledger" in view


def test_an_empty_view_still_round_trips() -> None:
    assert parse_ledger_view(render_ledger_view([])) == []
