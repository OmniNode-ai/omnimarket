"""Counts and public pin provenance are part of the acceptance contract."""

import re
from pathlib import Path

from omnimarket.handlers.work_contracts.loader import load_corpus

ROOT = Path(__file__).parents[2]
CORPUS = ROOT / "src/omnimarket/configs/work_contracts"


def test_corpus_loads_exact_scenarios_and_dod():
    contracts = load_corpus(CORPUS, input_root=ROOT)
    scenarios = {
        c.contract_id for c in contracts if re.fullmatch(r"F\d\d-\d\d", c.contract_id)
    }
    assert scenarios == {
        f"F{family:02}-{case:02}" for family in range(1, 11) for case in range(1, 11)
    }
    assert sum(c.contract_id.startswith("dod-") for c in contracts) >= 20
    for contract in contracts:
        assert contract.controls
        assert contract.provenance
        if contract.acceptance.kind != "schema":
            assert contract.evaluators
            assert contract.acceptance.spec.argv


def test_public_inputs_only():
    for contract in load_corpus(CORPUS, input_root=ROOT):
        for pin in (*contract.inputs, *contract.evaluators):
            assert not any(
                x in pin.path.lower()
                for x in [
                    "_".join(("knowledge", "base", "internal")),
                    "customer",
                    "secret",
                    ".env",
                    "credential",
                ]
            )
            assert (ROOT / pin.path).resolve().is_relative_to(ROOT.resolve())
