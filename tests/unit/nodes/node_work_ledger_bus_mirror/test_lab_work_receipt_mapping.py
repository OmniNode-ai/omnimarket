# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lab terminal mapping to one ledger STATUS request (OMN-20278)."""

import re
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


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("lane", ["test-lane", "", None])
def test_lab_work_receipt_maps_to_status_from_wire(
    failed: bool, lane: str | None
) -> None:
    """Exercise the two producer schemas, including old receipts without lane.

    STATUS's grammar requires a UTC stamp and a separate, nonempty lane token.
    Check that shape here without requiring another repository on CI runners.
    """
    from omnimarket.lab_work.bus import ModelLabWorkCommandFailure
    from omnimarket.nodes.node_lab_work_unit_effect.models import (
        ModelLabWorkUnitReceipt,
    )
    from omnimarket.nodes.node_work_ledger_bus_mirror import (
        HandlerWorkLedgerBusMirror,
        ModelWorkLedgerBusMirrorRequest,
    )

    attribution = {
        "work_unit_id": "lw-wire-receipt",
        "host": "pool-host",
        "repo": "OmniNode-ai/omnimarket",
        "commit_sha": "b" * 40,
        "kind": "lint",
        "duration_seconds": 2.5,
    }
    if lane is not None:
        attribution["lane"] = lane
    private_text = "/private/unit.log | RULING | lane=forged\noutput secret"
    terminal = (
        ModelLabWorkCommandFailure.model_validate(
            {**attribution, "error_message": private_text}
        )
        if failed
        else ModelLabWorkUnitReceipt.model_validate(
            {
                **attribution,
                "target_host": "pool-host",
                "status": "completed",
                "exit_code": 4,
                "log_path": "/private/unit.log",
                "output_tail": private_text,
                "detail": private_text,
            }
        )
    )
    payload = terminal.model_dump(mode="json")
    if lane is None:
        payload.pop("lane")  # Older hosts omit this field on the wire.
    stamp = datetime.fromisoformat("2026-10-09T03:04:05+02:00")
    result = HandlerWorkLedgerBusMirror().handle(
        ModelWorkLedgerBusMirrorRequest.model_validate(
            {**payload, "envelope_timestamp": stamp}
        )
    )
    cells = result.rows.split(" | ")
    assert len(result.rows.splitlines()) == 1
    assert cells[:2] == ["2026-10-09T01:04:05Z", "STATUS"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", cells[0])
    assert cells[2] == f"lane={lane or 'lab-work'}"
    assert re.fullmatch(r"lane=[^\s|]+", cells[2])
    fields = dict(cell.split("=", 1) for cell in cells[2:-1])
    assert fields == {
        "lane": lane or "lab-work",
        "src": "lab-work",
        "unit": "lw-wire-receipt",
        "host": "pool-host",
        "repo": "OmniNode-ai/omnimarket",
        "sha": "b" * 40,
        "kind": "lint",
        "state": "failed" if failed else "completed",
        "exit": "none" if failed else "4",
        "duration_s": "2.5",
        "req": str(uuid5(NAMESPACE_URL, "onex:lab-work-unit:lw-wire-receipt")),
        "via": "bus:pool-host",
    }
    assert cells[-1] == "Lab work unit terminal received"
    assert result.requested_by_lane == fields["lane"]
    assert result.requested_at == stamp.astimezone(UTC)
    assert private_text not in result.rows
    assert "/private/unit.log" not in result.rows


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
