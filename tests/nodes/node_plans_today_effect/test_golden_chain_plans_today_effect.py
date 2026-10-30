# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node is registered, and its packaged contract invokes with a typed request in a member's session."""

from __future__ import annotations

import importlib
import json
import shutil
import tomllib
from importlib.metadata import entry_points
from pathlib import Path
from typing import get_type_hints

import pytest
import yaml
from pydantic import BaseModel

from tests.nodes.node_plans_today_effect.conftest import (
    IDENTITY_FIXTURE,
    MEMBER_A_LINEAR,
    MEMBER_A_PRINCIPAL,
    MEMBER_B_LINEAR,
    NOW,
    body,
    issue,
    kb_fixture,
    ledger,
    row,
)

pytestmark = pytest.mark.unit

NODE = "node_plans_today_effect"
PACKAGE = f"omnimarket.nodes.{NODE}"
REPO = Path(__file__).resolve().parents[3]


def test_node_is_registered_as_an_onex_node() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["entry-points"]["onex.nodes"][NODE] == PACKAGE
    installed = {ep.name: ep.value for ep in entry_points(group="onex.nodes")}
    if NODE in installed:
        assert installed[NODE] == PACKAGE
    package = importlib.import_module(PACKAGE)
    assert package.NodePlansTodayEffect.contract_path.is_file()
    assert importlib.import_module(f"{PACKAGE}.__main__") is not None


def test_contract_golden_chain_in_a_members_session(tmp_path: Path) -> None:
    package = importlib.import_module(PACKAGE)
    assert package.__file__ is not None
    here = Path(package.__file__).parent
    contract = yaml.safe_load((here / "contract.yaml").read_text())
    metadata = yaml.safe_load((here / "metadata.yaml").read_text())
    assert contract["name"] == metadata["name"] == NODE
    assert metadata["entry_points"]["onex.nodes"][NODE] == PACKAGE
    assert contract["metadata"]["related_tickets"] == ["OMN-21392"]
    binding = contract["handler"]
    handler_class = getattr(
        importlib.import_module(binding["module"]), binding["class"]
    )
    assert (
        contract["handler_routing"]["default_handler"]
        == binding["module"] + ":" + binding["class"]
    )

    def model(ref: str) -> type[BaseModel]:
        module, _, name = ref.rpartition(".")
        result = getattr(importlib.import_module(module), name)
        assert issubclass(result, BaseModel)
        assert result.model_config == {"frozen": True, "extra": "forbid"}
        return result

    request_class = model(binding["input_model"])
    result_class = model(binding["output_model"])
    hints = get_type_hints(handler_class.handle)
    assert hints["request"] is request_class
    assert hints["return"] is result_class
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}

    roots = tmp_path / "roots"
    (roots / "team_identity").mkdir(parents=True)
    shutil.copyfile(IDENTITY_FIXTURE, roots / "team_identity" / "overlay.yaml")
    _, kb = kb_fixture(
        tmp_path,
        {
            "2026-09-28-two.md": body(
                "Two", [("mine", "OMN-3001"), ("theirs", "OMN-3002")]
            )
        },
    )
    rows = tmp_path / "rows.txt"
    rows.write_text(
        ledger(
            [
                row(
                    1,
                    "2026-09-29T15:50:00Z",
                    "CLAIM",
                    "lane-a",
                    "OMN-3001",
                    principal=MEMBER_A_PRINCIPAL,
                )
            ]
        )
    )
    snapshot = tmp_path / "linear-2026-09-29.txt"
    snapshot.write_text('{"issues": []}')
    snapshot.with_suffix(".json").write_text(
        json.dumps(
            {
                "subject": MEMBER_A_LINEAR,
                "issues": [
                    issue("OMN-3001", prs=[]),
                    issue("OMN-3002", assignee=MEMBER_B_LINEAR, prs=[]),
                ],
            }
        )
    )
    result = handler_class().handle(
        request_class.model_validate(
            {
                "kb": str(kb),
                "no_fetch": True,
                "linear_snapshot": str(snapshot),
                "ledger_rows_file": str(rows),
                "summaries": "none",
                "no_msg": True,
                "out_dir": str(tmp_path / "out"),
                "now": NOW,
                "home": str(tmp_path),
                "overlay_roots": [str(roots)],
            }
        )
    )
    assert isinstance(result, result_class)
    assert result.exit_code == 0
    assert result.refused is None
    assert result.mode == "member"
    doc = result.document
    assert doc is not None
    assert doc["scope"]["kind"] == "member"
    assert doc["scope"]["tickets"] == ["OMN-3001"]
    assert doc["sources"]["pr_source"] == "gh-pr-view"
    assert doc["sources"]["pr_reads"] == 0
    tasks = {t["label"]: t for t in doc["plans"][0]["tasks"]}
    assert tasks["T1"]["state"] == "not-started"
    assert tasks["T1"]["in_scope"] is True
    assert tasks["T2"]["state"] == "unread"
    assert tasks["T2"]["in_scope"] is False
    assert doc["plans"][0]["next_action_ticket"] == "OMN-3001"
    assert "scope: member fixture-member-a" in result.stdout
