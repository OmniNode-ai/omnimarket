# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The GLM not-logged-in item of contracts/OMN-26043.yaml binds a criterion.

Repo-evidence dod-verify counts an item that binds no acceptance criterion as non-probative, so a pull
request whose only new item is unbound adds no evidence of its own. The item must name AC5 and keep the
focused test as its check.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "OMN-26043.yaml"
ITEM_ID = "dod-omn26043-glm-harness-not-logged-in"


def _item() -> dict[str, Any]:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    (item,) = [e for e in document["dod_evidence"] if e["id"] == ITEM_ID]
    return item


def test_glm_item_binds_ac5() -> None:
    assert _item().get("binds_ac") == ["AC5"]


def test_glm_item_does_not_claim_to_bind_nothing() -> None:
    assert "binds no criterion" not in _item()["description"]


def test_glm_item_keeps_its_focused_check() -> None:
    (check,) = _item()["checks"]
    assert check["check_type"] == "test_passes"
    assert "test_harness_process_runner_omn26219.py" in check["check_value"]
    assert "glm_without_declared_route" in check["check_value"]
