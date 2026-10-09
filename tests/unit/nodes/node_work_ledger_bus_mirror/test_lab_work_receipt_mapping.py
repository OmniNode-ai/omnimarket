# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lab terminal mapping to one ledger STATUS request (OMN-20278)."""

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

import pytest
from pydantic import ValidationError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [
        ("completed", 0),
        ("completed", 4),
        ("failed", None),
        ("timed_out", None),
        ("refused", None),
        ("infra_error", None),
    ],
)
@pytest.mark.parametrize("lane", ["test-lane", ""])
def test_lab_work_receipt_maps_to_status(
    status: str, exit_code: int | None, lane: str
) -> None:
    from omnimarket.nodes.node_work_ledger_bus_mirror import (
        HandlerWorkLedgerBusMirror,
        ModelWorkLedgerBusMirrorRequest,
    )

    request = ModelWorkLedgerBusMirrorRequest(
        work_unit_id="lw-0123456789abcdef",
        host="pool-host",
        lane=lane,
        repo="OmniNode-ai/omnimarket",
        commit_sha="a" * 40,
        kind="test",
        status=status,
        exit_code=exit_code,
        duration_seconds=1.25,
        envelope_timestamp=datetime(2026, 10, 9, tzinfo=UTC),
    )
    result = HandlerWorkLedgerBusMirror().handle(request)
    cells = result.rows.split(" | ")
    assert cells[:2] == ["2026-10-09T00:00:00Z", "STATUS"]
    assert f"lane={lane or 'lab-work'}" in cells
    assert "src=lab-work" in cells
    assert "unit=lw-0123456789abcdef" in cells
    assert "host=pool-host" in cells
    assert "repo=OmniNode-ai/omnimarket" in cells
    assert f"sha={'a' * 40}" in cells
    assert "kind=test" in cells
    assert f"state={status}" in cells
    assert f"exit={exit_code if exit_code is not None else 'none'}" in cells
    assert "duration_s=1.25" in cells
    assert result.request_id == uuid5(
        NAMESPACE_URL, "onex:lab-work-unit:lw-0123456789abcdef"
    )
    assert f"req={result.request_id}" in cells
    assert result.requested_by_lane == (lane or "lab-work")
    assert result.requesting_host == "pool-host"
    assert result.requested_at == request.envelope_timestamp
    assert "\n" not in result.rows
    assert result == HandlerWorkLedgerBusMirror().handle(request)


@pytest.mark.parametrize(
    "field", ["lane", "work_unit_id", "host", "repo", "commit_sha"]
)
def test_mapping_refuses_ledger_cell_injection(field: str) -> None:
    from omnimarket.nodes.node_work_ledger_bus_mirror import (
        ModelWorkLedgerBusMirrorRequest,
    )

    data = {
        "work_unit_id": "lw-0123456789abcdef",
        "host": "pool-host",
        "status": "failed",
        "envelope_timestamp": datetime(2026, 10, 9, tzinfo=UTC),
        field: "bad | lane=forged\nrow",
    }
    with pytest.raises(ValidationError):
        ModelWorkLedgerBusMirrorRequest.model_validate(data)
