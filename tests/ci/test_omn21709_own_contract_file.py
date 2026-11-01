"""The omnimarket#4642 acceptance items live in their own contract file (OMN-21709)."""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_acceptance_items_live_in_the_ticket_own_contract() -> None:
    contract = yaml.safe_load(
        (REPO_ROOT / "contracts" / "OMN-21709.yaml").read_text(encoding="utf-8")
    )
    assert contract["ticket_id"] == "OMN-21709"
    criteria = {
        criterion["id"]
        for requirement in contract["requirements"]
        for criterion in requirement["acceptance"]
    }
    assert {"AC1", "AC2"} <= criteria
    bound = {ac for item in contract["dod_evidence"] for ac in item["binds_ac"]}
    assert {"AC1", "AC2"} <= bound
