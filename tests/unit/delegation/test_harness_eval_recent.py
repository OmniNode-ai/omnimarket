# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Receipt window, independent checker controls and source identity proofs."""

import json
from collections import Counter
from datetime import datetime, timedelta

import pytest

from tests.harness_evals.fixtures import TASK_TYPES, files_for, oracle
from tests.harness_evals.recent import MANIFEST, recent_tasks
from tests.harness_evals.recent import check_task as check

pytestmark = pytest.mark.unit


def test_recent_cohort_has_one_dated_receipt_per_task_type():
    manifest = json.loads(MANIFEST.read_text())
    start = datetime.fromisoformat(manifest["window_start"])
    end = datetime.fromisoformat(manifest["window_end"])
    assert end - start == timedelta(days=1)
    tasks = recent_tasks()
    assert Counter(t["task_class"] for t in tasks) == dict.fromkeys(TASK_TYPES, 1)
    assert len({t["item_key"] for t in tasks}) == len(TASK_TYPES)
    for task in tasks:
        source = task["source"]
        assert start <= datetime.fromisoformat(source["ended_at"]) <= end
        assert len(bytes.fromhex(source["sha256"])) == 32
        assert source["adaptation"]


@pytest.mark.parametrize("task", recent_tasks(), ids=lambda t: t["item_key"])
def test_recent_checkers_reject_initial_accept_oracle_and_reject_tampering(
    task, tmp_path
):
    files, _ = files_for(task)
    for name, value in files.items():
        (tmp_path / name).write_text(value)
    assert not check(task, tmp_path)
    oracle(task, tmp_path)
    assert check(task, tmp_path)
    protected = next(
        (name for name in files if name not in task["expected_files"]),
        next(iter(files)),
    )
    (tmp_path / protected).write_text("tampered")
    assert not check(task, tmp_path)


def test_recent_json_grading_accepts_equivalent_formatting(tmp_path):
    task = next(t for t in recent_tasks() if t["task_class"] == "ci-log-diagnosis")
    for name, value in files_for(task)[0].items():
        (tmp_path / name).write_text(value)
    expected = json.loads(task["expected_files"]["answer.json"])
    (tmp_path / "answer.json").write_text(json.dumps(expected, indent=2))
    assert check(task, tmp_path)
    expected["backend"] = "wrong-backend"
    (tmp_path / "answer.json").write_text(json.dumps(expected))
    assert not check(task, tmp_path)


def test_collector_keeps_attempt_numbers_unique_between_cohorts(tmp_path, monkeypatch):
    import sys

    from omnimarket.handlers.handler_harness_eval import ModelHarnessObservation
    from tests.harness_evals.collect import main
    from tests.harness_evals.fixtures import fixed_tasks

    versions = tmp_path / "versions.json"
    versions.write_text("{}")
    offset = 0
    attempts = set()
    for cohort, tasks in (("fixed", fixed_tasks()), ("recent", recent_tasks())):
        trial = tmp_path / (cohort + "-input")
        trial.mkdir()
        rows = []
        for task in tasks:
            item = trial / task["item_key"]
            item.mkdir()
            (item / "process.log").write_text("")
            rows.append(
                ModelHarnessObservation(
                    item_key=task["item_key"],
                    task_class=task["task_class"],
                    harness="claude-glm",
                    model="test-model",
                    configuration=cohort,
                    duration_s=1,
                    passed=True,
                    harness_succeeded=True,
                ).model_dump_json()
            )
        (trial / "observations.jsonl").write_text("\n".join(rows) + "\n")
        (trial / "provenance.json").write_text("{}")
        output = tmp_path / (cohort + "-output")
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "collect",
                "--trial",
                str(trial),
                "--output",
                str(output),
                "--tenant-id",
                "00000000-0000-0000-0000-000000000001",
                "--lane",
                "same-lane",
                "--ticket",
                "OMN-21396",
                "--harness-versions",
                str(versions),
                "--cohort",
                cohort,
                "--attempt-offset",
                str(offset),
            ],
        )
        assert main() == 0
        emitted = json.loads((output / "experiment-rows.json").read_text())
        current = {(row["lane"], row["attempt"]) for row in emitted}
        assert len(current) == len(tasks)
        assert not attempts.intersection(current)
        attempts.update(current)
        assert len((output / "experiment-status.txt").read_text().splitlines()) == len(
            tasks
        )
        offset += len(tasks)
