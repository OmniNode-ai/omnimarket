# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-22867: the two causes that red Tests (Split 5/20) and (Split 19/20) stay fixed."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE_CONTRACT = REPO_ROOT / "contracts" / "OMN-22795.yaml"
PROBE_ITEM = "dod-omn22795-integration-proof"


def test_omn22795_probe_item_carries_no_empty_binds_ac() -> None:
    contract = yaml.safe_load(PROBE_CONTRACT.read_text(encoding="utf-8"))
    items = {item["id"]: item for item in contract["dod_evidence"]}
    assert "binds_ac" not in items[PROBE_ITEM]


def test_durations_supplement_has_provenance_entry() -> None:
    provenance = (
        REPO_ROOT / "config" / "test_file_durations_provenance.json"
    ).read_text(encoding="utf-8")
    assert "supplement_omn22867_split5_freshness" in provenance
