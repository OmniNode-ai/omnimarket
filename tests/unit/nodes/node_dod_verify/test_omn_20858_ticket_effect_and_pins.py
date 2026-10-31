# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20858 -- the ticket read, and the pins a contract records.

OMN-21676: the read goes through the project tracker the ticketing nodes use, served
here by a recorded tracker, so no request leaves the machine and no credential is read. The pin rules mirror
``onex_change_control``'s stale-pin gate: a hash that moved is stale unless an
independent acceptance pins the live text; a retired binding is not a pin; a
record carrying no hash is not comparable.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnimarket.enums.enum_criteria_drift_kind import EnumCriteriaDriftKind
from omnimarket.nodes.node_dod_verify.models.model_ticket_criteria_read import (
    ModelTicketCriteriaRead,
)
from omnimarket.nodes.node_dod_verify.services.criteria_drift import (
    compare_criteria,
    extract_criteria_pins,
)
from omnimarket.nodes.node_dod_verify.services.criteria_ticket_read import (
    TrackerCriteriaReader,
)
from omnimarket.occ_contract_pin import criterion_hash

pytestmark = pytest.mark.unit

_KEY = "-".join(["recorded", "stand", "in", "value"])


class _Issue:
    def __init__(self, identifier: str, description: str | None) -> None:
        self.identifier = identifier
        self.description = description


class _Tracker:
    """A recorded tracker: serves one issue, or raises what it is given."""

    def __init__(self, answer: _Issue | BaseException) -> None:
        self._answer = answer
        self.asked: list[str] = []
        self.closed = False

    async def get_issue(self, issue_id: str) -> _Issue:
        self.asked.append(issue_id)
        if isinstance(self._answer, BaseException):
            raise self._answer
        return self._answer

    async def close(self) -> None:
        self.closed = True


def _reader(tracker: _Tracker) -> TrackerCriteriaReader:
    async def factory() -> _Tracker:
        return tracker

    return TrackerCriteriaReader(factory)


def test_the_reader_returns_the_ticket_body_and_closes_the_tracker() -> None:
    tracker = _Tracker(_Issue("OMN-1", "- AC1: x"))
    read = _reader(tracker).read("OMN-1")
    assert read == ModelTicketCriteriaRead(ticket_id="OMN-1", description="- AC1: x")
    assert tracker.asked == ["OMN-1"]
    assert tracker.closed


def test_the_reader_reads_a_null_description_as_an_empty_body() -> None:
    assert _reader(_Tracker(_Issue("OMN-1", None))).read("OMN-1").description == ""


def test_the_reader_without_a_tracker_is_unavailable_and_names_only_the_class() -> None:
    async def factory() -> _Tracker:
        raise RuntimeError(f"api_key_ref resolved to None {_KEY}")

    read = TrackerCriteriaReader(factory).read("OMN-1")
    assert read.description is None
    assert read.unavailable_reason == "no ticket tracker could be built (RuntimeError)"
    assert _KEY not in repr(read)


@pytest.mark.parametrize(
    "answer",
    [
        KeyError("Issue not found: OMN-1"),
        ConnectionError(f"unreachable {_KEY}"),
        _Issue("OMN-2", "- AC1: x"),
    ],
)
def test_the_reader_refuses_every_unusable_answer(
    answer: _Issue | BaseException,
) -> None:
    read = _reader(_Tracker(answer)).read("OMN-1")
    assert read.description is None
    assert read.unavailable_reason
    assert _KEY not in repr(read)


def test_the_reader_runs_inside_a_running_loop() -> None:
    async def inside() -> ModelTicketCriteriaRead:
        return _reader(_Tracker(_Issue("OMN-1", "- AC1: x"))).read("OMN-1")

    assert asyncio.run(inside()).description == "- AC1: x"


def test_a_read_carries_exactly_one_of_body_and_reason() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        ModelTicketCriteriaRead(ticket_id="OMN-1")
    with pytest.raises(ValueError, match="exactly one"):
        ModelTicketCriteriaRead(
            ticket_id="OMN-1", description="", unavailable_reason="x"
        )


# -- pins ---------------------------------------------------------------------


def _record(label: str, text: str, **fields: str) -> dict[str, str]:
    return {
        "label": label,
        "criterion_hash": criterion_hash(text),
        "proposed_by": "occ-autobind",
        "accepted_by": "author-uuid",
        **fields,
    }


def _contract(*items: dict[str, Any]) -> dict[str, Any]:
    return {"ticket_id": "OMN-1", "dod_evidence": list(items)}


def _compare(
    items: list[dict[str, Any]], body: str
) -> dict[str, EnumCriteriaDriftKind]:
    contract = _contract(*items)
    pins = extract_criteria_pins(contract, items)
    assert pins is not None
    drift, _ = compare_criteria(pins, "OMN-1", body)
    return {d.label: d.kind for d in drift}


def test_a_contract_that_records_no_criteria_has_no_pins() -> None:
    items = [{"id": "i", "binds_ac": ["AC1"]}]
    assert extract_criteria_pins(_contract(*items), items) is None


def test_a_record_with_no_hash_is_not_comparable() -> None:
    items = [
        {
            "id": "i",
            "binds_ac": ["AC1"],
            "ac_bindings": [{"label": "AC1", "accepted_by": "x", "proposed_by": "y"}],
        }
    ]
    assert (
        _compare(items, "## Acceptance criteria\n- AC1: whatever it says now\n") == {}
    )


def test_a_retired_binding_is_not_a_pin() -> None:
    old = _record(
        "AC1", "AC1: the old text", proposed_by="lane=draft", accepted_by="lane=draft"
    )
    items = [
        {"id": "draft", "binds_ac": ["AC1"], "ac_bindings": [old]},
        {
            "id": "carrier",
            "supersedes_ac_binding": [
                {
                    "item": "draft",
                    "label": "AC1",
                    "reason_kind": "no_longer_applicable",
                    "reason": "withdrawn after the criterion was rewritten",
                    "retired_by": "lane-x",
                    "retired_at": "2026-10-10T00:00:00Z",
                }
            ],
        },
    ]
    pins = extract_criteria_pins(_contract(*items), items)
    assert pins is not None
    assert pins.bindings == ()


def test_a_stale_record_beside_a_re_accepted_one_is_not_reported() -> None:
    new = "AC1: the new text"
    items = [
        {"id": "old", "binds_ac": ["AC1"], "ac_bindings": [_record("AC1", "AC1: old")]},
        {"id": "new", "binds_ac": ["AC1"], "ac_bindings": [_record("AC1", new)]},
    ]
    assert _compare(items, f"## Acceptance criteria\n- {new}\n") == {}


def test_a_stale_record_beside_a_self_accepted_one_is_reported() -> None:
    new = "AC1: the new text"
    items = [
        {"id": "old", "binds_ac": ["AC1"], "ac_bindings": [_record("AC1", "AC1: old")]},
        {
            "id": "new",
            "binds_ac": ["AC1"],
            "ac_bindings": [
                _record("AC1", new, proposed_by="lane=a", accepted_by="lane=a")
            ],
        },
    ]
    assert _compare(items, f"## Acceptance criteria\n- {new}\n") == {
        "AC1": EnumCriteriaDriftKind.EDITED
    }


def test_labels_are_compared_in_canonical_form() -> None:
    text = "**AC-1** -- the text"
    items = [
        {
            "id": "i",
            "binds_ac": ["ac-1"],
            "ac_bindings": [_record("ac-1", "AC-1 -- the text")],
        }
    ]
    # The contract spells the label ``ac-1``; the ticket spells it ``**AC-1**``.
    assert _compare(items, f"## Acceptance criteria\n- {text}\n") == {
        "AC1": EnumCriteriaDriftKind.EDITED
    }


def test_a_requirements_only_contract_compares_labels() -> None:
    contract = {
        "ticket_id": "OMN-1",
        "requirements": [
            {"id": "r", "acceptance": [{"id": "AC1", "statement": "AC1: a"}]}
        ],
        "dod_evidence": [{"id": "i", "binds_ac": ["AC1"]}],
    }
    pins = extract_criteria_pins(contract, contract["dod_evidence"])
    assert pins is not None
    assert pins.bindings == ()
    drift, _ = compare_criteria(pins, "OMN-1", "## Acceptance criteria\n- AC1: a\n")
    assert drift == ()
    drift, _ = compare_criteria(pins, "OMN-1", "## Acceptance criteria\n- AC2: b\n")
    assert {d.label: d.kind for d in drift} == {
        "AC1": EnumCriteriaDriftKind.DELETED,
        "AC2": EnumCriteriaDriftKind.ADDED,
    }
