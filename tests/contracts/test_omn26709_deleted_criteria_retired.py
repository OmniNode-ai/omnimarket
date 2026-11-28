"""OMN-26709 retires the binding of the criterion its ticket no longer carries.

The ticket carries AC1, AC2 and AC3. The contract's AC4 item claims a criterion
deleted from the ticket, so dod-verify reports CRITERIA_DRIFT until that binding
is retired with a typed supersedes_ac_binding entry.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "OMN-26709.yaml"
_DELETED = {("dod-omn26709-undeclared-broker-injected-overlay", "AC4")}


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


def test_the_deleted_ac4_binding_is_retired() -> None:
    assert _retirements() == _DELETED
