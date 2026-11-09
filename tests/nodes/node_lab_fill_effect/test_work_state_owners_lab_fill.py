# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab fill reads the work-state projection for who owns a ticket (OMN-22935 AC1).

The projection node writes every work item with the live lane its CLAIM cells attribute to it; the fill's owner facts read
that file beside the claim store, and the plan's ownership verdict skips a ticket the projection says a live lane holds.
An unread projection is never a free ticket: a file that exists and cannot be read is `owned-unreadable`, and one not yet
published adds no reading. The failing control is the same ledger without the CLAIM's ticket cell.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.work_state_projection import (
    ModelTicketSource,
    ModelWorkScope,
    ModelWorkStateRequest,
    ModelWorkStateResult,
    ModelWorkStateRules,
)
from omnimarket.nodes.node_lab_fill_effect.handlers import HandlerLabFillOwnerFacts
from omnimarket.nodes.node_lab_fill_effect.models import ModelLabFillOwnerFactsRequest
from omnimarket.nodes.node_lab_fill_effect.protocols import LabFillClaimFacts
from omnimarket.nodes.node_lab_fill_effect.protocols.local_lab_fill_adapters import (
    LocalOwnerReader,
)
from omnimarket.nodes.node_lab_fill_plan_compute.handlers import (
    HandlerLabFillOwnership,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models.model_lab_fill_ownership import (
    ModelLabFillOwnershipRequest,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    evidence_row_from_text,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.handler_work_state_projection import (
    HandlerWorkStateProjection,
)
from tests.nodes.node_lab_fill_effect.fakes import FakeClock, FakeOwners

pytestmark = pytest.mark.unit

EXAMPLE_OVERLAY = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_work_state_projection_compute/overlay.example.yaml"
)
RULES = load_structured_overlay(
    "node_work_state_projection_compute",
    ModelWorkStateRules,
    explicit=yaml.safe_load(EXAMPLE_OVERLAY.read_text(encoding="utf-8")),
)
LANES = [
    {
        "lane": "fill-omn-7",
        "ticket": "OMN-7",
        "pr": "",
        "repo": "omnimarket",
        "kind": "ticket",
    },
    {
        "lane": "fill-omn-8",
        "ticket": "OMN-8",
        "pr": "",
        "repo": "omnimarket",
        "kind": "ticket",
    },
]


@dataclass
class ProjectionOwners(FakeOwners):
    """The claim store is empty; the work-state reading is the production adapter's, over the file it is given."""

    def work_state_owners(
        self, state_path: str, max_age_s: float
    ) -> Mapping[str, Sequence[str]] | None:
        return LocalOwnerReader().work_state_owners(state_path, max_age_s)


def projection(now: datetime, ticket_cell: str) -> ModelWorkStateResult:
    stamp = (now - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    claim = evidence_row_from_text(
        f"{stamp} | CLAIM | lane=peer-lane | {ticket_cell} | actor=claude | model=sonnet | working it"
    )
    assert claim is not None
    return HandlerWorkStateProjection().handle(
        ModelWorkStateRequest(
            now=now,
            rules=RULES,
            scope=ModelWorkScope(),
            tickets=(
                ModelTicketSource(ticket_id="OMN-7", state_type="started"),
                ModelTicketSource(ticket_id="OMN-8", state_type="started"),
            ),
            ledger_rows=(claim,),
        )
    )


def verdicts(path: Path) -> Any:
    owners = ProjectionOwners(
        claims_facts=LabFillClaimFacts(index={}, open_claims=[], staleness_hours=12),
        registry={},
    )
    facts = HandlerLabFillOwnerFacts(reader=owners, clock=FakeClock()).handle(
        ModelLabFillOwnerFactsRequest.model_validate(
            {"lanes": LANES, "ledger_path": "/l", "work_state_path": str(path)}
        )
    )
    return HandlerLabFillOwnership().handle(
        ModelLabFillOwnershipRequest.model_validate(
            {"lanes": LANES, **facts.model_dump(mode="json")}
        )
    )


def test_golden_a_ticket_a_live_lane_claims_is_skipped_as_owned(tmp_path: Path) -> None:
    path = tmp_path / "work-state.json"
    path.write_text(
        projection(datetime.now(UTC), "ticket=OMN-7").model_dump_json(),
        encoding="utf-8",
    )
    result = verdicts(path)
    assert [p.ticket for p in result.proceed] == ["OMN-8"]
    skipped = result.skipped[0]
    assert skipped["lane"] == "fill-omn-7"
    assert str(skipped["skipped"]).startswith("owned")
    assert "work-state=owned:peer-lane" in str(skipped["detail"])


def test_error_the_control_without_the_ticket_cell_leaves_both_tickets_free(
    tmp_path: Path,
) -> None:
    path = tmp_path / "work-state.json"
    path.write_text(
        projection(datetime.now(UTC), "tickez=OMN-7").model_dump_json(),
        encoding="utf-8",
    )
    result = verdicts(path)
    assert [p.ticket for p in result.proceed] == ["OMN-7", "OMN-8"]


def test_error_an_unreadable_projection_is_not_a_free_ticket(tmp_path: Path) -> None:
    path = tmp_path / "work-state.json"
    path.write_text("{not json", encoding="utf-8")
    result = verdicts(path)
    assert result.proceed == ()
    assert {str(s["skipped"]) for s in result.skipped} == {"owned-unreadable"}


def test_error_a_stale_projection_is_not_a_free_ticket(tmp_path: Path) -> None:
    path = tmp_path / "work-state.json"
    old = datetime.now(UTC) - timedelta(hours=3)
    path.write_text(projection(old, "ticket=OMN-7").model_dump_json(), encoding="utf-8")
    result = verdicts(path)
    assert result.proceed == ()
    assert {str(s["skipped"]) for s in result.skipped} == {"owned-unreadable"}


def test_a_projection_not_yet_published_adds_no_reading(tmp_path: Path) -> None:
    result = verdicts(tmp_path / "absent.json")
    assert [p.ticket for p in result.proceed] == ["OMN-7", "OMN-8"]
