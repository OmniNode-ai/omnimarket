"""The work contract corpus loads: every pinned input matches the file on disk."""

from pathlib import Path

from omnimarket.handlers.work_contracts.loader import load_corpus

ROOT = Path(__file__).parents[2]
CORPUS = ROOT / "src/omnimarket/configs/work_contracts"


def test_work_contract_corpus_pins_match_tree() -> None:
    contracts = load_corpus(CORPUS, input_root=ROOT)
    assert contracts
