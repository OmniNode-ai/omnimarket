# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The header of each recorded-day fixture states only the scrubbing the fixture really has."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FOREIGN_REPOSITORY = re.compile(r"\bomni(?:claude|base_\w+|intelligence|memory)\w*")
CLAIM = "repository names other than omnimarket are replaced"


def _split(name: str) -> tuple[str, str]:
    lines = (FIXTURES / name).read_text(encoding="utf-8").splitlines()
    header = [line for line in lines if line.startswith("#")]
    body = [line for line in lines if not line.startswith("#")]
    return " ".join(header), "\n".join(body)


@pytest.mark.parametrize(
    "name", ["recorded_day_archive_split.md", "recorded_day_ledger.md"]
)
def test_header_does_not_claim_a_scrub_the_body_contradicts(name: str) -> None:
    header, body = _split(name)
    assert FOREIGN_REPOSITORY.search(body), (
        "the fixture keeps no foreign repository name"
    )
    assert CLAIM not in " ".join(header.split())
    assert "retained as recorded" in header
