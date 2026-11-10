# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review findings of OMN-22573: the byte budget is measured in bytes, and the dev probe fails when its predicate is false."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_PG_TEST = (
    _ROOT
    / "tests/test_omn22478_quality_gate_failure_categories_bounded_real_postgres.py"
)
_CONTRACT = _ROOT / "contracts/OMN-22573.yaml"


def _probe_command() -> str:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    (command,) = [
        check["check_value"]
        for item in contract["dod_evidence"]
        if item["id"] == "dod-omn22573-dev-read-after-roll"
        for check in item["checks"]
    ]
    return str(command)


def test_the_postgres_test_measures_the_list_in_bytes() -> None:
    source = _PG_TEST.read_text(encoding="utf-8")
    assert "octet_length(failure_categories::text)" in source
    assert re.search(r"(?<![a-z_])length\(failure_categories", source) is None


def test_the_dev_probe_measures_the_list_in_bytes() -> None:
    probe = _probe_command()
    assert "octet_length(failure_categories::text)" in probe
    assert re.search(r"(?<![a-z_])length\(failure_categories", probe) is None


def test_the_dev_probe_exits_non_zero_when_no_row_meets_the_predicate() -> None:
    """A bare ``select count(*)`` exits 0 whether the count is 0 or 1; dividing by it errors, and psql exits 1, at 0."""
    assert "select 1/count(*)" in _probe_command()
