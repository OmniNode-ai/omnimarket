"""The earlier OMN-24967 contract cites the 2026-11-15 RULING."""

from pathlib import Path

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "OMN-24967.yaml"


def test_contract_cites_the_2026_11_15_ruling() -> None:
    assert "RULING 2026-11-15" in CONTRACT.read_text(encoding="utf-8")
