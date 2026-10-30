# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The packaged node decides through the in-memory local runtime, from planted fixtures."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

from omnimarket.handlers.wip_admission_overlay import (
    load_wip_admission_config,
)
from omnimarket.models.wip_admission import ModelWipAdmissionDecision
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_wip_admission_compute/contract.yaml"
)


def lim(limit: int) -> dict[str, object]:
    return {
        "limit": limit,
        "merges_per_hour": 1.0,
        "lead_time_hours": 3.0,
        "method": "m",
        "evidence": "e",
    }


@pytest.mark.parametrize(
    ("kind", "open_prs", "outcome", "reason"),
    [
        ("new_work", 2, "admit", "under_limit"),
        ("new_work", 3, "refuse", "at_repo_limit"),
        ("ticket_create", 3, "admit", "ticket_record"),
    ],
)
def test_decision_through_runtime_local(
    tmp_path: Path, kind: str, open_prs: int, outcome: str, reason: str
) -> None:
    read = {
        "readable": True,
        "read_at": NOW.isoformat(),
        "items": [
            {"kind": "pull_request", "repo": "fixture-repo", "ref": f"fixture-repo#{i}"}
            for i in range(open_prs)
        ],
    }
    input_path = tmp_path / "input.json"
    input_path.write_text(
        json.dumps(
            {
                "kind": kind,
                "repo": "fixture-repo",
                "now": NOW.isoformat(),
                "read": read,
                "policy": {
                    "enforce_ruling": "fixture ruling",
                    "fleet": lim(9),
                    "limits": {"fixture-repo": lim(3)},
                    "fix_forward_classes": ["red"],
                },
                "config": load_wip_admission_config().model_dump(),
            }
        )
    )
    runtime = RuntimeLocal(
        workflow_path=CONTRACT,
        state_root=tmp_path / "state",
        input_path=input_path,
        timeout=30,
    )
    assert runtime.run() == EnumWorkflowResult.COMPLETED
    written = json.loads((tmp_path / "state" / "workflow_result.json").read_text())
    terminal = written.get("terminal_payload") or written["handler_result"]
    decision = ModelWipAdmissionDecision.model_validate(terminal)
    assert (decision.outcome.value, decision.reason.value) == (outcome, reason)
