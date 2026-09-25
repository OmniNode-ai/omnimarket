# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-19428 -- the verifier accepts the fields the OCC evidence item model owns.

`onex_change_control` added `supersedes_ac_binding` to its evidence item model
(`ModelDodEvidenceItem`): the record by which a later item retires a criterion
binding that a merged, immutable item got wrong. This collector refuses an item
carrying any field outside its accepted set with `INVALID_DOD_EVIDENCE_ITEM`,
and that refusal fails EVERY evidence check on the contract. It had learned
`binds_ac` and `ac_bindings` one literal at a time and had not learned this one,
so every contract that retires a binding -- the parent ticket's among them --
could never verify.

The fix is not a sixth literal. The OCC-local fields are read from
`ModelDodEvidenceItem` in the contract's own OCC tree, so the next field that
model gains is accepted on the day it merges. These tests exercise that read
path through a stand-in OCC tree, never through a pinned field list.

RED before the change:
`test_a_contract_retiring_a_binding_is_not_rejected` failed with
`INVALID_DOD_EVIDENCE_ITEM: strict canonical field set rejected unknown
field(s): 'supersedes_ac_binding'=...`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)
from tests.unit.nodes.node_dod_verify.omn_19428_occ_tree import (
    FIXTURES,
    MODEL_RELPATH,
    occ_contract,
    occ_tree,
)

pytestmark = pytest.mark.unit

_HASH = "9d006777c2e97aabd867d0c48c23fac73e0608770843ba66a24fcd093b73080f"

# A stand-in for the OCC model. Its shape is the real one's (a pydantic model
# class with annotated fields and a model_config); its field list is whatever a
# test needs it to be.
_STAND_IN_MODEL = '''\
from pydantic import BaseModel, ConfigDict


class ModelDodCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    check_type: str
    check_value: str


class ModelDodEvidenceItem(BaseModel):
    """Stand-in."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    description: str
{extra_fields}
    checks: list[ModelDodCheck] = []
'''


@pytest.fixture(autouse=True)
def _no_ambient_occ_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """The OCC tree under test is the one the contract lives in, never the host's."""
    for name in ("CONTRACT_REPO_DIR", "ONEX_CC_REPO_PATH", "OMNI_HOME"):
        monkeypatch.delenv(name, raising=False)


def _stand_in(extra_fields: tuple[str, ...]) -> str:
    body = "\n".join(f"    {name}: tuple[object, ...] = ()" for name in extra_fields)
    return _STAND_IN_MODEL.format(extra_fields=body)


def _bound_item() -> dict[str, object]:
    return {
        "id": "dod-old",
        "description": "the item whose binding is retired",
        "binds_ac": ["AC1"],
        "ac_bindings": [
            {
                "label": "AC1",
                "criterion_hash": _HASH,
                "accepted_by": "an-approving-reviewer",
                "accepted_at": "2026-09-12T21:00:00Z",
            }
        ],
        "checks": [{"check_type": "command", "check_value": "true"}],
    }


def _retiring_item() -> dict[str, object]:
    return {
        "id": "dod-new",
        "description": "retires dod-old's AC1 binding",
        "supersedes_ac_binding": [
            {
                "item": "dod-old",
                "label": "AC1",
                "reason": "its check read the tree; the criterion is settled live",
            }
        ],
        "checks": [{"check_type": "command", "check_value": "true"}],
    }


_REAL_SHAPE = ("binds_ac", "ac_bindings", "supersedes_ac_binding")


def _messages(results: list[object]) -> list[str]:
    return [str(getattr(result, "message", "") or "") for result in results]


def test_a_contract_retiring_a_binding_is_not_rejected(tmp_path: Path) -> None:
    """THE POINT: the record that retires a wrong binding must not fail the contract."""
    path = occ_contract(tmp_path, [_bound_item(), _retiring_item()])

    results = EvidenceCollector().collect("OMN-9999", contract_path=path)

    assert results
    rejections = [m for m in _messages(results) if "INVALID_DOD_EVIDENCE_ITEM" in m]
    assert rejections == []


def test_a_genuinely_unknown_field_is_still_rejected(tmp_path: Path) -> None:
    """The control. Reading the authority must not open the gate."""
    item = _retiring_item()
    item["not_a_real_field"] = "x"
    path = occ_contract(tmp_path, [item])

    results = EvidenceCollector().collect("OMN-9999", contract_path=path)

    assert any(
        "INVALID_DOD_EVIDENCE_ITEM" in m and "not_a_real_field" in m
        for m in _messages(results)
    )


def test_authority_a_field_the_occ_model_does_not_declare_is_refused(
    tmp_path: Path,
) -> None:
    """The field set follows the model: drop it from the model and it is refused."""
    path = occ_contract(
        tmp_path,
        [_bound_item(), _retiring_item()],
        model_source=_stand_in(("binds_ac", "ac_bindings")),
    )

    results = EvidenceCollector().collect("OMN-9999", contract_path=path)

    assert any(
        "INVALID_DOD_EVIDENCE_ITEM" in m and "supersedes_ac_binding" in m
        for m in _messages(results)
    )


def test_authority_a_field_the_occ_model_gains_is_accepted_without_a_code_change(
    tmp_path: Path,
) -> None:
    """The reason for deriving: the next OCC field needs no edit here."""
    item = _retiring_item()
    item["a_field_added_next_month"] = []
    path = occ_contract(
        tmp_path,
        [_bound_item(), item],
        model_source=_stand_in((*_REAL_SHAPE, "a_field_added_next_month")),
    )

    results = EvidenceCollector().collect("OMN-9999", contract_path=path)

    assert results
    assert not any("INVALID_DOD_EVIDENCE_ITEM" in m for m in _messages(results))


def test_authority_an_unreadable_model_refuses_the_contract_by_name(
    tmp_path: Path,
) -> None:
    """No model, no guess: the contract is refused, and the refusal names the file."""
    path = occ_contract(tmp_path, [_bound_item(), _retiring_item()])
    (tmp_path / "onex_change_control" / MODEL_RELPATH).unlink()

    results = EvidenceCollector().collect("OMN-9999", contract_path=path)

    assert results
    assert all(result.status.value == "failed" for result in results)
    messages = _messages(results)
    assert any(
        "OCC_DOD_ITEM_FIELD_AUTHORITY_UNREADABLE" in m
        and "ModelDodEvidenceItem" in m
        and str(MODEL_RELPATH) in m
        for m in messages
    ), messages


def test_authority_a_model_without_the_class_refuses_the_contract_by_name(
    tmp_path: Path,
) -> None:
    path = occ_contract(tmp_path, [_retiring_item()], model_source="X = 1\n")

    results = EvidenceCollector().collect("OMN-9999", contract_path=path)

    assert any(
        "OCC_DOD_ITEM_FIELD_AUTHORITY_UNREADABLE" in m and "ModelDodEvidenceItem" in m
        for m in _messages(results)
    )


def test_authority_a_core_only_contract_needs_no_occ_tree(tmp_path: Path) -> None:
    """Items inside the installed core model's fields never consult the OCC tree.

    A contract outside any OCC tree, with no OCC root in the environment, still
    verifies as before -- the OCC model is read only for a field the core model
    does not own.
    """
    path = tmp_path / "OMN-9999.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "ticket_id": "OMN-9999",
                "dod_evidence": [
                    {
                        "id": "dod-core",
                        "description": "core fields only",
                        "binds_ac": ["AC1"],
                        "checks": [{"check_type": "command", "check_value": "true"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    results = EvidenceCollector().collect("OMN-9999", contract_path=str(path))

    messages = _messages(results)
    assert not any("INVALID_DOD_EVIDENCE_ITEM" in m for m in messages)
    assert not any("OCC_DOD_ITEM_FIELD_AUTHORITY_UNREADABLE" in m for m in messages)


# ---------------------------------------------------------------------------
# The corpus. Every OCC contract that carried the string at the pinned commit,
# read against the REAL OCC model at that same commit (verbatim fixture, blob
# ae576b07). The fixture records each item's field names and execution scope,
# which is everything the field gate reads.
# ---------------------------------------------------------------------------

_CORPUS = json.loads(
    (FIXTURES / "omn_19428_supersedes_ac_binding_corpus.json").read_text(
        encoding="utf-8"
    )
)
_CARRYING = {
    ticket
    for ticket, record in _CORPUS["contracts"].items()
    if any("supersedes_ac_binding" in item["fields"] for item in record["items"])
}


def test_corpus_names_the_contracts_that_carry_the_field() -> None:
    """Seven carry the string; five carry it as an item field."""
    assert len(_CORPUS["contracts"]) == 7
    assert {
        "OMN-15359",
        "OMN-16504",
        "OMN-17214",
        "OMN-18033",
        "OMN-18580",
    } == _CARRYING


@pytest.mark.parametrize("ticket", sorted(_CORPUS["contracts"]))
def test_corpus_every_item_passes_the_field_gate(ticket: str, tmp_path: Path) -> None:
    root = occ_tree(tmp_path)
    items: list[object] = []
    for record in _CORPUS["contracts"][ticket]["items"]:
        item: dict[str, object] = dict.fromkeys(record["fields"])
        item["id"] = record["id"]
        item["description"] = record["id"]
        item.pop("execution_scope", None)
        if "execution_scope" in record:
            item["execution_scope"] = record["execution_scope"]
        items.append(item)

    failures = EvidenceCollector._validate_evidence_audiences(items, root)

    assert [f.message for f in failures] == []
