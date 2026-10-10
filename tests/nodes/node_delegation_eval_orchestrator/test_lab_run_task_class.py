# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The committed lab command can re-run the eval on named task classes only."""

import json
from typing import Any

import pytest

from omnimarket.nodes.node_delegation_eval_orchestrator import lab_run

pytestmark = pytest.mark.unit

TENANT = "11111111-1111-1111-1111-111111111111"


def _row(item_key: str, task_class: str, answer: str) -> dict[str, Any]:
    return {
        "item_key": item_key,
        "task_class": task_class,
        "stratum": f"{task_class}/accepted",
        "label": "adequate",
        "prompt_snapshot": "Reply with the single word: alive.",
        "response_snapshot": answer,
        "gate_verdict": "accepted",
        "deciding_check": None,
    }


class _Writer:
    def bind_projection_database_url(self, url: str) -> None:
        self.url = url

    def handle(self, payload: dict[str, Any]) -> dict[str, int]:
        return {"rows_upserted": len(payload["item_verdicts"])}


def _main(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *extra: str,
) -> dict[str, Any]:
    rows = [
        _row("item-a", "test", "alive"),
        _row("item-b", "summarization", "alive"),
        _row("item-c", "review", "alive"),
    ]

    async def read_labelled(args: object, tenant: str) -> list[dict[str, Any]]:
        return rows

    monkeypatch.setattr(lab_run, "_read_labelled", read_labelled)
    monkeypatch.setattr(lab_run, "_dsn", lambda _name: "postgresql://unused")
    monkeypatch.setattr(lab_run, "DelegationEvalProjectionWriter", _Writer)
    argv = [
        "--tenant-id",
        TENANT,
        "--read-dsn-env",
        "READ_DSN",
        "--write-dsn-env",
        "WRITE_DSN",
        "run",
        "--manifest-id",
        "manifest",
        "--rater-role",
        "human",
        "--rubric-version",
        "v1",
        "--gate-version",
        "gate",
        *extra,
    ]
    assert lab_run.main(argv) == 0
    receipt: dict[str, Any] = json.loads(capsys.readouterr().out)
    return receipt


def test_run_task_class_keeps_only_the_named_classes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    receipt = _main(
        monkeypatch, capsys, "--task-class", "test", "--task-class", "review"
    )
    assert receipt["labelled_items"] == 2
    assert {row["task_class"] for row in receipt["results"]} == {"test", "review"}


def test_run_without_task_class_reads_every_class(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    receipt = _main(monkeypatch, capsys)
    assert receipt["labelled_items"] == 3
    assert {row["task_class"] for row in receipt["results"]} == {
        "test",
        "summarization",
        "review",
    }
