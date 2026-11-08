"""Three dod entries of contracts/OMN-19998.yaml keep the cwd their checks were written with.

omnimarket#5078, #5103 and #5112 merged with the ``cwd: .`` line of these entries
dropped by a textual union merge of the shared contract (OMN-22546). The closer
binding rule ``no-cwd`` refuses a command check that names no working directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "OMN-19998.yaml"
_ENTRIES = (
    "dod-omn19998-lane-failure-runtime",
    "dod-omn19998-automatic-fix-install-deploy",
    "dod-omn19998-brief-frozen-destinations-deploy",
)


@pytest.mark.parametrize("entry_id", _ENTRIES)
def test_entry_checks_name_their_working_directory(entry_id: str) -> None:
    document = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    entries = [e for e in document["dod_evidence"] if e["id"] == entry_id]
    assert len(entries) == 1, f"{entry_id} is not exactly one entry"
    checks = entries[0]["checks"]
    assert checks, f"{entry_id} has no checks"
    for check in checks:
        assert check.get("cwd") == ".", f"{entry_id} check names no cwd: {check}"
