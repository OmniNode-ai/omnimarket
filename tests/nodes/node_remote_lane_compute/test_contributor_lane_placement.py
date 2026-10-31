# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A collaborator machine takes no house-lane work and runs its lanes on its own lane (OMN-21599).

Operator RULING 2026-10-30T16:45:31Z: work may run on a collaborator's machine, but not in the lane
the house works in and breaks; the collaborators have a lane of their own. The pool reading of a
host marked ``contributor`` carries the lane its row binds (``bus_lane``) and whether that lane can
take a lane (``lane_ready``). The planted cases below are the ruling's: a house-lane lane is refused
on a contributor host, and a lane bound to the collaborator lane is admitted once that lane is live.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.overlay_keys.model_overlay_documents import ModelLabHostsTable
from omnimarket.nodes.node_remote_lane_compute.handlers.handler_remote_lane_placement import (
    HandlerRemoteLanePlacement,
    contributor_lane_refusal,
)
from omnimarket.nodes.node_remote_lane_compute.models import (
    CODEX_ENGINE,
    EnumRemoteLaneOutcome,
    ModelRemoteLaneHostReading,
    ModelRemoteLanePlacementRequest,
    ModelRemoteLanePlacementResult,
)

HOUSE = "house"
COLLAB = "collab"
NOT_READY = "the collaborator lane's broker takes no login yet"


def _reading(name: str, **kwargs: object) -> ModelRemoteLaneHostReading:
    base: dict[str, object] = {
        "name": name,
        "engines": ("claude_sonnet", CODEX_ENGINE),
        "lane_slots": 4,
        "lane_cap": 6,
        "rank_free": 5.0,
        "bus_lane": HOUSE,
    }
    base.update(kwargs)
    return ModelRemoteLaneHostReading.model_validate(base)


def _contributor(**kwargs: object) -> ModelRemoteLaneHostReading:
    base: dict[str, object] = {
        "contributor": True,
        "bus_lane": COLLAB,
        "lane_ready": True,
        "rank_free": 50.0,
    }
    base.update(kwargs)
    return _reading("guest", **base)


def _place(
    *readings: ModelRemoteLaneHostReading,
    lane: str | None = None,
    engine: str = "claude_sonnet",
) -> ModelRemoteLanePlacementResult:
    request = ModelRemoteLanePlacementRequest(
        engine=engine, readings=tuple(readings), lane=lane
    )
    return HandlerRemoteLanePlacement().handle(request)


def _reason(result: ModelRemoteLanePlacementResult, host: str) -> str:
    return next(v.reason for v in result.verdicts if v.host == host)


@pytest.mark.unit
def test_a_house_lane_lane_is_refused_on_a_contributor_host() -> None:
    """The planted refusal: the lane is bound to the house lane, the contributor host is the better host."""
    result = _place(_reading("lab"), _contributor(), lane=HOUSE)
    assert result.host == "lab"
    assert "house-bound lane is refused" in _reason(result, "guest")


@pytest.mark.unit
def test_a_house_lane_lane_with_only_a_contributor_host_places_nowhere() -> None:
    result = _place(_contributor(), lane=HOUSE)
    assert result.host is None
    assert result.outcome == EnumRemoteLaneOutcome.NO_HOST
    assert "its lanes bind collab only" in _reason(result, "guest")


@pytest.mark.unit
def test_a_collaborator_bound_lane_is_admitted_when_the_collaborator_lane_is_live() -> (
    None
):
    """The planted admission: bound to the collaborator lane, which can take a lane."""
    result = _place(_contributor(), lane=COLLAB)
    assert result.host == "guest"
    assert result.outcome == EnumRemoteLaneOutcome.ADMITTED


@pytest.mark.unit
def test_a_lane_that_names_no_lane_binds_the_hosts_own_and_is_admitted_when_live() -> (
    None
):
    assert _place(_contributor()).host == "guest"


@pytest.mark.unit
def test_the_host_takes_no_lane_while_the_collaborator_lane_cannot_take_one() -> None:
    """No broker auth yet: no lane, not a house-lane one, and the verdict says why."""
    result = _place(
        _contributor(lane_ready=False, lane_not_ready_reason=NOT_READY), lane=COLLAB
    )
    assert result.host is None
    assert result.outcome == EnumRemoteLaneOutcome.NO_HOST
    reason = _reason(result, "guest")
    assert "cannot take a lane yet" in reason
    assert NOT_READY in reason


@pytest.mark.unit
def test_lane_readiness_fails_closed_without_a_declared_reason() -> None:
    result = _place(_contributor(lane_ready=False))
    assert result.host is None
    assert "no reason declared" in _reason(result, "guest")


@pytest.mark.unit
def test_an_unready_contributor_host_leaves_the_lane_to_the_house_hosts() -> None:
    result = _place(
        _reading("lab"), _contributor(lane_ready=False, lane_not_ready_reason=NOT_READY)
    )
    assert result.host == "lab"
    assert NOT_READY in _reason(result, "guest")


@pytest.mark.unit
def test_a_contributor_row_that_binds_the_house_lane_takes_no_lane() -> None:
    """A mis-declared row (the lane the house hosts bind) is refused whatever the request names."""
    result = _place(_reading("lab"), _contributor(bus_lane=HOUSE), lane=HOUSE)
    assert result.host == "lab"
    assert "the lane the house hosts bind" in _reason(result, "guest")


@pytest.mark.unit
def test_a_contributor_row_that_declares_no_lane_takes_no_lane() -> None:
    result = _place(_contributor(bus_lane=None))
    assert result.host is None
    assert "declares no lane to bind" in _reason(result, "guest")


@pytest.mark.unit
def test_the_codex_engine_is_judged_by_the_same_rule() -> None:
    house_bound = _place(_contributor(), lane=HOUSE, engine=CODEX_ENGINE)
    assert house_bound.host is None
    bound = _place(_contributor(), lane=COLLAB, engine=CODEX_ENGINE)
    assert bound.host == "guest"


@pytest.mark.unit
def test_a_host_that_is_not_a_contributor_is_judged_as_before() -> None:
    reading = _reading("lab")
    request = ModelRemoteLanePlacementRequest(
        engine="claude_sonnet", readings=(reading,), lane=COLLAB
    )
    assert contributor_lane_refusal(request, reading) is None
    assert _place(reading, lane=COLLAB).host == "lab"


@pytest.mark.unit
def test_an_old_producer_without_the_new_fields_places_as_before() -> None:
    reading = ModelRemoteLaneHostReading(
        name="lab", engines=("claude_sonnet",), lane_slots=2, lane_cap=4
    )
    result = HandlerRemoteLanePlacement().handle(
        ModelRemoteLanePlacementRequest(engine="claude_sonnet", readings=(reading,))
    )
    assert result.host == "lab"


def _table(*hosts: dict[str, object]) -> dict[str, object]:
    return {"hosts": [{"target": "x", **h} for h in hosts]}


@pytest.mark.unit
def test_the_host_table_accepts_a_contributor_row_on_its_own_lane() -> None:
    table = ModelLabHostsTable.model_validate(
        _table(
            {"name": "lab", "ledger_bus_lane": HOUSE},
            {
                "name": "guest",
                "contributor": True,
                "ledger_bus_lane": HOUSE,
                "lane_binding": COLLAB,
                "lane_ready": False,
                "lane_not_ready_reason": NOT_READY,
            },
        )
    )
    assert table.hosts[1].lane_ready is False


@pytest.mark.unit
@pytest.mark.parametrize(
    ("guest", "message"),
    [
        ({"lane_binding": HOUSE, "lane_ready": True}, "the lane the non-contributor"),
        ({"ledger_bus_lane": HOUSE, "lane_ready": True}, "declares no lane_binding"),
        ({"lane_binding": COLLAB}, "declares no lane_not_ready_reason"),
    ],
)
def test_the_host_table_refuses_a_contributor_row_that_could_take_house_work(
    guest: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ModelLabHostsTable.model_validate(
            _table(
                {"name": "lab", "ledger_bus_lane": HOUSE},
                {"name": "guest", "contributor": True, **guest},
            )
        )
