"""OMN-21710 keeps its acceptance items in its own contract file."""

from __future__ import annotations

from pathlib import Path

import yaml

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"


def _load(name: str) -> dict:
    return yaml.safe_load((CONTRACTS / name).read_text(encoding="utf-8"))


def test_own_contract_declares_both_criteria() -> None:
    contract = _load("OMN-21710.yaml")
    ids = {
        criterion["id"]
        for requirement in contract["requirements"]
        for criterion in requirement["acceptance"]
    }
    assert ids == {"AC1", "AC2"}


def test_shared_parent_contract_carries_no_omn21710_items() -> None:
    parent = (CONTRACTS / "OMN-21536.yaml").read_text(encoding="utf-8")
    assert "omn21710" not in parent.lower()
