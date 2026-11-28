"""OMN-25104 retires the bindings of criteria its ticket no longer carries.

The ticket carries AC1, AC2, AC3 and AC5. The contract's AC4 and AC7 items
claim criteria deleted from the ticket, so dod-verify reports CRITERIA_DRIFT
until each is retired with a typed supersedes_ac_binding entry.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "OMN-25104.yaml"
_DELETED = {
    ("dod-omn25104-ac4", "AC4"),
    ("dod-omn25104-ac7", "AC7"),
}


def _retirements() -> set[tuple[str, str]]:
    document = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    found: set[tuple[str, str]] = set()
    for entry in document["dod_evidence"]:
        for retired in entry.get("supersedes_ac_binding", []):
            assert retired["reason_kind"] == "no_longer_applicable"
            assert retired["reason"]
            assert retired["retired_by"]
            assert retired["retired_at"]
            found.add((retired["item"], retired["label"]))
    return found


def test_the_deleted_ac4_and_ac7_bindings_are_retired() -> None:
    assert _retirements() == _DELETED
