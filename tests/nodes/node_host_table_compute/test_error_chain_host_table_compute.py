# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain of the host table compute and of the board's reader of it (OMN-22251)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_factory_floor_effect.source_loader import load_host_table
from omnimarket.nodes.node_host_table_compute.handlers.handler_host_table import (
    HandlerHostTable,
    HostTableRefusedError,
)
from omnimarket.nodes.node_host_table_compute.models import ModelHostTableRequest
from tests.nodes.node_host_table_compute.table_support import (
    EXCLUDED,
    row,
    table,
    write_table,
)

pytestmark = pytest.mark.unit

UNREASONED = row(EXCLUDED, available=False)


def test_an_excluded_row_with_no_reason_is_refused() -> None:
    request = ModelHostTableRequest(table=table(row("admitted-a"), UNREASONED))
    with pytest.raises(HostTableRefusedError) as refused:
        HandlerHostTable().handle(request)
    assert EXCLUDED in str(refused.value)
    assert "excluded without a stated reason" in str(refused.value)


def test_a_blank_reason_is_no_reason() -> None:
    blank = row(EXCLUDED, available=False, unavailable_reason="   ")
    with pytest.raises(HostTableRefusedError, match="excluded without a stated reason"):
        HandlerHostTable().handle(ModelHostTableRequest(table=table(blank)))


def test_a_host_named_twice_is_refused() -> None:
    request = ModelHostTableRequest(table=table(row("twice"), row("twice")))
    with pytest.raises(HostTableRefusedError, match="duplicate host names: twice"):
        HandlerHostTable().handle(request)


def test_the_board_reads_no_table_when_no_overlay_is_found(tmp_path: Path) -> None:
    assert load_host_table({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}) is None


def test_the_board_reads_no_table_when_an_exclusion_states_no_reason(
    tmp_path: Path,
) -> None:
    env = write_table(tmp_path / "hosts.yaml", row("admitted-a"), UNREASONED)
    assert load_host_table(env) is None
