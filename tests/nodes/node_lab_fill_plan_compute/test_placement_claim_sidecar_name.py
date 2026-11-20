# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The placement CLAIM carries the sidecar lane name, so the ticket-creation gate finds its commitment (OMN-26216)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.handlers.ledger_row_grammar import parse_row
from omnimarket.nodes.node_lab_fill_plan_compute.handlers.placement_claim_binding import (
    commitment_bound,
    placement_claim_row,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "refused_sidecar_creates_2026-11-20.json"
# The recorded CLAIM of 2026-11-20T12:01:15Z, as written: the sidecar name was place-evalsovl-1225.
RECORDED_WRONG_NAME_CLAIM = (
    "2026-11-20T12:01:15Z | CLAIM | lane=evalsovl | ticket=OMN-25856 | actor=claude:sonnet:subagent"
    " | model=claude-sonnet-5-5 | est ~0.5 lane-hours; displaces other swarm placement lane time"
    " on the operator Mac; (OMN-25856) | worktree=none | item=throughput | parent=orchestrator-31d9-swarm"
)


def claim_for(sidecar_lane: str, at: datetime) -> str:
    return placement_claim_row(
        at=at,
        sidecar_lane=sidecar_lane,
        ticket="OMN-26216",
        host="h201",
        run="rlane-replay-0001",
        parent="orchestrator-31d9-swarm",
        cost_hours=0.5,
        displaces="a lane on the busy launching host",
        scope="placement CLAIM under the sidecar name",
    )


def refused_payloads() -> list[dict[str, str]]:
    data = json.loads(FIXTURE.read_text())
    rows: list[dict[str, str]] = data["rows"]
    return rows


def test_sidecar_name_replay_the_recorded_wrong_name_claim_binds_nothing() -> None:
    """Red control: the CLAIM that existed under another name left the sidecar lane unbound (the refusal)."""
    assert not commitment_bound([RECORDED_WRONG_NAME_CLAIM], "place-evalsovl-1225")
    assert commitment_bound([RECORDED_WRONG_NAME_CLAIM], "evalsovl")


def test_sidecar_name_replay_gives_zero_refusals() -> None:
    payloads = refused_payloads()
    assert len(payloads) == 43, "the fixture is the 2026-11-20 sidecar-lane refusals"
    refused_before = [
        p for p in payloads if not commitment_bound([], p["sidecar_lane"])
    ]
    assert len(refused_before) == len(payloads), (
        "positive control: with no CLAIM every payload is refused"
    )
    refused_after = []
    for payload in payloads:
        at = datetime.strptime(payload["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        row = claim_for(payload["sidecar_lane"], at)
        if not commitment_bound([row], payload["sidecar_lane"]):
            refused_after.append(payload)
    assert refused_after == []


def test_sidecar_name_replay_claim_lane_cell_is_the_whole_sidecar_name() -> None:
    for sidecar in (
        "place-evalsovl-1225",
        "place-batch0-0215-PLACEMENT:-remote-lane",
        "integration:act184",
    ):
        row = claim_for(sidecar, datetime(2026, 11, 20, 12, 0, tzinfo=UTC))
        parsed = parse_row(row, types=frozenset({"CLAIM"}))
        assert parsed is not None
        assert parsed.value("lane") == sidecar
        assert parsed.value("host") == "h201"
        assert parsed.value("run") == "rlane-replay-0001"
        # A stem or a prefix of the sidecar name binds nothing.
        assert not commitment_bound([row], sidecar[:-1])
        assert not commitment_bound([row], sidecar + "-x")


def test_sidecar_name_replay_a_terminal_ends_the_commitment() -> None:
    sidecar = "place-evalsovl-1225"
    claim = claim_for(sidecar, datetime(2026, 11, 20, 12, 0, tzinfo=UTC))
    terminal = f"2026-11-20T13:00:00Z | TERMINAL | lane={sidecar} | outcome=done | friction=none"
    assert commitment_bound([claim], sidecar)
    assert not commitment_bound([claim, terminal], sidecar)


def test_sidecar_name_replay_refuses_a_name_that_is_not_one_ledger_cell() -> None:
    with pytest.raises(ValueError, match="sidecar_lane"):
        claim_for("place-x | lane=other", datetime(2026, 11, 20, 12, 0, tzinfo=UTC))
