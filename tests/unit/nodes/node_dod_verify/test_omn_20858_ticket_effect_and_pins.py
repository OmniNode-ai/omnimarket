# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20858 -- the Linear ticket read, and the pins a contract records.

The read is exercised through ``urllib.request.urlopen`` patched at the module
boundary, so no request leaves the machine. The pin rules mirror
``onex_change_control``'s stale-pin gate: a hash that moved is stale unless an
independent acceptance pins the live text; a retired binding is not a pin; a
record carrying no hash is not comparable.
"""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any

import pytest

from omnimarket.enums.enum_criteria_drift_kind import EnumCriteriaDriftKind
from omnimarket.nodes.node_dod_verify.handlers import (
    handler_dod_ticket_criteria_linear_effect as effect_module,
)
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_ticket_criteria_linear_effect import (
    HandlerDodTicketCriteriaLinearEffect,
)
from omnimarket.nodes.node_dod_verify.models.model_ticket_criteria_read import (
    ModelTicketCriteriaRead,
)
from omnimarket.nodes.node_dod_verify.services.criteria_drift import (
    compare_criteria,
    extract_criteria_pins,
)
from omnimarket.occ_contract_pin import criterion_hash

pytestmark = pytest.mark.unit

_KEY = "-".join(["recorded", "stand", "in", "value"])


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _answer(monkeypatch: pytest.MonkeyPatch, body: object) -> list[Any]:
    sent: list[Any] = []

    def fake_urlopen(request: Any, timeout: float) -> _Response:
        sent.append(request)
        return _Response(json.dumps(body).encode("utf-8"))

    monkeypatch.setattr(effect_module.urllib.request, "urlopen", fake_urlopen)
    return sent


def test_the_effect_returns_the_ticket_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", _KEY)
    sent = _answer(
        monkeypatch,
        {"data": {"issue": {"identifier": "OMN-1", "description": "- AC1: x"}}},
    )
    read = HandlerDodTicketCriteriaLinearEffect().read("OMN-1")
    assert read == ModelTicketCriteriaRead(ticket_id="OMN-1", description="- AC1: x")
    assert json.loads(sent[0].data)["variables"] == {"id": "OMN-1"}


def test_the_effect_reads_a_null_description_as_an_empty_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", _KEY)
    _answer(
        monkeypatch, {"data": {"issue": {"identifier": "OMN-1", "description": None}}}
    )
    assert HandlerDodTicketCriteriaLinearEffect().read("OMN-1").description == ""


def test_the_effect_without_a_credential_is_unavailable_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    sent = _answer(monkeypatch, {})
    read = HandlerDodTicketCriteriaLinearEffect().read("OMN-1")
    assert read.description is None
    assert read.unavailable_reason == "no Linear credential in the environment"
    assert sent == []


@pytest.mark.parametrize(
    "body",
    [
        {"errors": [{"message": "boom"}]},
        {"data": {"issue": None}},
        {"data": {"issue": {"identifier": "OMN-2", "description": "- AC1: x"}}},
        ["not", "an", "object"],
    ],
)
def test_the_effect_refuses_every_unusable_answer(
    monkeypatch: pytest.MonkeyPatch, body: object
) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", _KEY)
    _answer(monkeypatch, body)
    read = HandlerDodTicketCriteriaLinearEffect().read("OMN-1")
    assert read.description is None
    assert read.unavailable_reason


def test_the_effect_names_a_transport_failure_without_the_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", _KEY)

    def broken(request: Any, timeout: float) -> Any:
        raise urllib.error.URLError(f"unreachable {request.headers}")

    monkeypatch.setattr(effect_module.urllib.request, "urlopen", broken)
    read = HandlerDodTicketCriteriaLinearEffect().read("OMN-1")
    assert read.unavailable_reason == "Linear read failed (URLError)"
    assert _KEY not in repr(read)


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
