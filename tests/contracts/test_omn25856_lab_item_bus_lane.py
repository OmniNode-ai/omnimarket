"""The lab-work integration-proof items of two tickets name their bus lane.

``onex lab-work run`` publishes over the shared kafka bus and refuses a call that
names no broker ("pass --lane <lane id>"); the local Done gate read that refusal
as a failed item for OMN-26726 and OMN-25827. Each item's command must carry
``--bus-lane dev`` between ``run`` and the first positional word, as the
OMN-26712 item does.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
_ITEMS = {
    "OMN-26726": "dod-omn26726-tick-integration-proof",
    "OMN-25827": "dod-omn25827-integration-proof",
}


def _words(ticket: str) -> list[str]:
    document = yaml.safe_load((_CONTRACTS / f"{ticket}.yaml").read_text("utf-8"))
    entries = [e for e in document["dod_evidence"] if e["id"] == _ITEMS[ticket]]
    assert len(entries) == 1, f"{_ITEMS[ticket]} is not exactly one entry"
    assert len(entries[0]["checks"]) == 1
    return shlex.split(entries[0]["checks"][0]["check_value"])


@pytest.mark.parametrize("ticket", sorted(_ITEMS))
def test_the_lab_work_call_names_the_dev_bus_lane(ticket: str) -> None:
    words = _words(ticket)
    call = words.index("lab-work")
    before_sent = words[call : words.index("--")]
    assert before_sent[1] == "run"
    assert "--bus-lane" in before_sent, before_sent
    assert before_sent[before_sent.index("--bus-lane") + 1] == "dev"


def test_the_evidence_item_binds_a_criterion_the_contract_declares() -> None:
    document = yaml.safe_load((_CONTRACTS / "OMN-25856.yaml").read_text("utf-8"))
    declared = {
        a["id"] for r in document["requirements"] for a in r.get("acceptance", [])
    }
    entries = [
        e
        for e in document["dod_evidence"]
        if e["id"] == "dod-omn25856-lab-item-bus-lane"
    ]
    assert len(entries) == 1
    bound = entries[0].get("binds_ac", [])
    assert bound, (
        "the item binds no criterion, so the hosted control has nothing to fail"
    )
    assert set(bound) <= declared, sorted(set(bound) - declared)
