# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fresh answers cannot inherit the old answer's adequate label."""

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_delegation_eval_orchestrator import lab_run
from tests.nodes.node_delegation_eval_run_orchestrator.test_backend_run import pin


def test_fresh_items_use_backend_pin_and_no_historical_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = pin()
    pin_path = tmp_path / "pin.json"
    pin_path.write_text(backend.model_dump_json())
    items_path = tmp_path / "items.jsonl"
    row = {
        "item_key": "i",
        "task_class": "reasoning",
        "stratum": "all",
        "label": "adequate",
        "prompt_snapshot": "Hello",
        "response_snapshot": "Hi",
        "latency_ms": 3.0,
        "backend_configuration": backend.model_dump(),
    }
    items_path.write_text(json.dumps(row) + "\n")
    captured = []

    class Writer:
        def bind_projection_database_url(self, dsn):
            pass

        def handle(self, payload):
            captured.append(payload)
            return {}

    monkeypatch.setattr(lab_run, "DelegationEvalProjectionWriter", Writer)
    monkeypatch.setattr(lab_run, "_dsn", lambda _name: "unused")
    assert (
        lab_run.main(
            [
                "--tenant-id",
                "11111111-1111-1111-1111-111111111111",
                "--read-dsn-env",
                "UNUSED",
                "--write-dsn-env",
                "WRITE",
                "run",
                "--manifest-id",
                "manifest",
                "--rater-role",
                "unlabelled:fresh",
                "--rubric-version",
                "v1",
                "--gate-version",
                "gate",
                "--generated-items",
                str(items_path),
                "--backend-pin",
                str(pin_path),
            ]
        )
        == 0
    )
    assert captured[0]["item_verdicts"][0]["label"] == "unlabelable"
    assert (
        captured[0]["item_verdicts"][0]["backend_configuration"] == backend.model_dump()
    )
    assert captured[0]["item_verdicts"][0]["latency_ms"] == 3.0
    assert all(
        r["false_pass_line_verdict"] == "refused" for r in captured[0]["results"]
    )
