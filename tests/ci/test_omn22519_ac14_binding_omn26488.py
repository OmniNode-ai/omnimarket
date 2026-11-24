# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-26488: the OMN-22519 AC14 evidence item declares a test_passes check."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "OMN-22519.yaml"
ITEM_ID = "dod-omn22519-ac14-accepted-20261124t1145z"


def test_omn22519_ac14_pytest_run_is_a_test_passes_check() -> None:
    data = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    items = [i for i in data["dod_evidence"] if i["id"] == ITEM_ID]
    assert len(items) == 1
    checks = items[0]["checks"]
    assert checks
    for check in checks:
        assert check["check_type"] == "test_passes"
        assert "pytest" in check["check_value"]
