"""OMN-24519: the five retired nodes stay absent from the tree and entry points."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

RETIRED = (
    "node_extraction_fold_compute",
    "node_extraction_verify_compute",
    "node_transcript_chunk_compute",
    "node_review_equivalence_reducer",
    "node_review_finding_identity_compute",
)


@pytest.mark.unit
@pytest.mark.parametrize("name", RETIRED)
def test_retired_node_directory_absent(name: str) -> None:
    assert not (ROOT / "src" / "omnimarket" / "nodes" / name).exists()


@pytest.mark.unit
@pytest.mark.parametrize("name", RETIRED)
def test_retired_node_entry_point_absent(name: str) -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(rf"^{name}\s*=", text, re.MULTILINE) is None
