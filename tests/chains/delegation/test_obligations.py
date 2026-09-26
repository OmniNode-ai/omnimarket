# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for walker-derived delegation chain obligations."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.chains.delegation.obligations import (
    WALKER_REPORT,
    load_obligations,
    reduce_walker_workflow,
)

pytestmark = pytest.mark.unit


def test_committed_obligation_report_has_expected_terminal_paths() -> None:
    report = json.loads(WALKER_REPORT.read_text(encoding="utf-8"))
    assert re.fullmatch(r"[0-9a-f]{40}", report["core_sha"])

    obligations = load_obligations()
    path_ids = [obligation.path_id for obligation in obligations]
    assert len(path_ids) == len(set(path_ids))
    assert sum(obligation.kind == "golden" for obligation in obligations) == 6
    assert sum(obligation.kind == "error" for obligation in obligations) == 5
    assert all(
        obligation.steps[-1][2] == "FAILED"
        for obligation in obligations
        if obligation.kind == "error"
    )
    assert all(
        obligation.steps[-1][2] == "COMPLETED"
        for obligation in obligations
        if obligation.kind == "golden"
    )


def test_reducer_deduplicates_reducer_only_interleavings() -> None:
    full_report = {
        "workflows": [
            {
                "workflow_owner": "owner",
                "components": [{"node": "owner"}, {"node": "reducer"}],
                "sync_triggers": ["sync"],
                "error_edges": [],
                "paths": [
                    {
                        "kind": "golden",
                        "steps": [
                            {
                                "from_state": ["START", "idle"],
                                "trigger": "advance",
                                "to_state": ["COMPLETED", "idle"],
                            }
                        ],
                    },
                    {
                        "kind": "golden",
                        "steps": [
                            {
                                "from_state": ["START", "idle"],
                                "trigger": "reducer_tick",
                                "to_state": ["START", "updated"],
                            },
                            {
                                "from_state": ["START", "updated"],
                                "trigger": "advance",
                                "to_state": ["COMPLETED", "updated"],
                            },
                        ],
                    },
                ],
            }
        ]
    }

    reduced = reduce_walker_workflow(
        full_report,
        workflow_owner="owner",
        core_sha="a" * 40,
        omnimarket_sha="b" * 40,
        walker_command="walker",
        full_report_sha256="c" * 64,
    )

    assert reduced["paths"] == [
        {
            "path_id": "golden:advance",
            "kind": "golden",
            "steps": [["START", "advance", "COMPLETED"]],
            "raw_path_count": 2,
        }
    ]


def test_load_obligations_rejects_duplicate_path_id(tmp_path: Path) -> None:
    report_path = tmp_path / "walker_report.json"
    duplicate = {
        "core_sha": "a" * 40,
        "paths": [
            {"path_id": "golden:advance", "kind": "golden", "steps": []},
            {"path_id": "golden:advance", "kind": "golden", "steps": []},
        ],
    }
    report_path.write_text(json.dumps(duplicate), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate path_id"):
        load_obligations(report_path)
