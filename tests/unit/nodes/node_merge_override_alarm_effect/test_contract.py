# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract names its routes, tables and secret."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit
NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_merge_override_alarm_effect"
)


def _contract() -> dict[str, Any]:
    raw = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert isinstance(raw, dict)
    return raw


def test_contract_declares_the_ledger_read_and_the_four_tables() -> None:
    tables = {t["name"]: t for t in _contract()["db_io"]["db_tables"]}
    assert tables["work_ledger_rows"]["access"] == "read"
    for name in (
        "merge_override_receipts",
        "merge_override_pending",
        "merge_override_verdicts",
        "merge_override_open_findings",
    ):
        assert tables[name]["access"] == "write"
        assert (NODE / "migrations" / tables[name]["migration"]).is_file()
        assert (
            f"CREATE TABLE IF NOT EXISTS {name} "
            in (NODE / "migrations" / tables[name]["migration"]).read_text()
        )


def test_contract_routes_each_input_model_once() -> None:
    handlers = _contract()["handler_routing"]["handlers"]
    models = [h["event_model"]["name"] for h in handlers]
    assert sorted(models) == sorted(set(models))
    assert set(models) == {
        "ModelPrMergedEvent",
        "ModelRepoMerged",
        "ModelPrLandingGithubCompleted",
        "ModelPrLandingGithubFailed",
        "ModelMergeOverrideSweepTrigger",
    }


def test_contract_settings_live_only_in_the_overlay() -> None:
    assert not (NODE / "overlay.yaml").exists()
    example = yaml.safe_load((NODE / "overlay.example.yaml").read_text())
    assert example["s19_repositories"] == []
