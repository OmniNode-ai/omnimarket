# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22389: the test-durations record is refreshed on dev before it hits the cap.

The freshness test (``test_ci_shard_plan_record_freshness_omn17427``) fails every
open PR once 50 test files are unrecorded, and it did so three times on
2026-11-03. ``node_test_durations_refresh_effect`` (``status`` and
``supplement``) plus ``.github/workflows/test-durations-refresh.yml`` keep the
record ahead of it.

Four properties, each with its own ``-k`` falsifier:

* ``status``: the count of unrecorded files, and the refresh being due at the
  threshold and not before.
* ``supplement``: the unrecorded files are added, every existing entry is left
  exactly as it was, and a second run changes nothing.
* ``threshold``: the refresh opens at under half the cap, and the cap is unchanged.
* ``workflow``: the workflow runs on a dev push, measures only when due, and
  opens or updates one PR.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_test_durations_refresh_effect.handlers.handler_test_durations_refresh import (
    UNMEASURED_FILE_SECONDS,
    HandlerTestDurationsRefresh,
)
from omnimarket.nodes.node_test_durations_refresh_effect.models.model_test_durations_refresh import (
    EnumTestDurationsOperation,
    ModelTestDurationsRefreshRequest,
)
from omnimarket.nodes.node_test_durations_refresh_effect.runtime_test_durations_refresh import (
    contract_refresh_at,
    discover_test_files,
    main,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "merge_test_durations.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-durations-refresh.yml"
FRESHNESS_TEST = (
    REPO_ROOT / "tests" / "unit" / "test_ci_shard_plan_record_freshness_omn17427.py"
)
NODE_MODULE = "omnimarket.nodes.node_test_durations_refresh_effect"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _tree(root: Path, names: list[str]) -> None:
    for name in names:
        target = root / "tests" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def test_x() -> None:\n    pass\n", encoding="utf-8")


def _repo(tmp_path: Path, recorded: int, unrecorded: int) -> dict[str, float]:
    """A tree of ``recorded + unrecorded`` test files; the record holds the first."""
    names = [f"test_f{index:03d}.py" for index in range(recorded + unrecorded)]
    _tree(tmp_path, names)
    record = {f"tests/{name}": 2.0 for name in names[:recorded]}
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "test_file_durations.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (tmp_path / "config" / "test_file_durations_provenance.json").write_text(
        json.dumps({"ticket": "OMN-19684"}, indent=2) + "\n", encoding="utf-8"
    )
    return record


def _run(*args: str, root: Path) -> int:
    return main(
        [
            args[0],
            "--root",
            str(root),
            "--record",
            str(root / "config" / "test_file_durations.json"),
            *args[1:],
        ]
    )


def _supplement_args(durations: Path, root: Path) -> tuple[str, ...]:
    return (
        "supplement",
        "--durations",
        str(durations),
        "--provenance",
        str(root / "config" / "test_file_durations_provenance.json"),
        "--base-sha",
        "abcdef0123456789abcdef0123456789abcdef01",
        "--runner",
        "unit test runner",
        "--recorded-on",
        "2026-11-03",
    )


# =============================================================================
# status
# =============================================================================


def test_status_counts_unrecorded_files_and_is_due_only_at_the_threshold() -> None:
    at = contract_refresh_at()
    files = tuple(f"tests/test_f{index:03d}.py" for index in range(at + 5))
    handler = HandlerTestDurationsRefresh()
    for unrecorded_count, due in ((at - 1, False), (at, True), (at + 3, True)):
        record = dict.fromkeys(files[: len(files) - unrecorded_count], 1.0)
        result = handler.handle(
            ModelTestDurationsRefreshRequest(
                operation=EnumTestDurationsOperation.STATUS,
                test_files=files,
                record=record,
                refresh_at=at,
            )
        )
        assert len(result.unrecorded) == unrecorded_count
        assert result.due is due
        assert result.updated_record is None


def test_status_cli_prints_counts_writes_the_list_and_the_github_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    at = contract_refresh_at()
    _repo(tmp_path, recorded=10, unrecorded=at)
    files_out = tmp_path / "unrecorded.txt"
    github_output = tmp_path / "out.txt"
    assert (
        _run(
            "status",
            "--files-out",
            str(files_out),
            "--github-output",
            str(github_output),
            root=tmp_path,
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["unrecorded"] == at
    assert report["test_files"] == 10 + at
    assert report["due"] is True
    listed = files_out.read_text(encoding="utf-8").splitlines()
    assert len(listed) == at
    assert listed[0] == "tests/test_f010.py"
    assert github_output.read_text(encoding="utf-8").splitlines() == [
        "due=true",
        f"unrecorded={at}",
    ]


def test_status_cli_is_not_due_one_file_below_the_threshold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _repo(tmp_path, recorded=10, unrecorded=contract_refresh_at() - 1)
    github_output = tmp_path / "out.txt"
    assert _run("status", "--github-output", str(github_output), root=tmp_path) == 0
    assert json.loads(capsys.readouterr().out)["due"] is False
    assert "due=false" in github_output.read_text(encoding="utf-8").splitlines()


def test_status_discovery_finds_the_files_the_planner_script_finds() -> None:
    script = _load(SCRIPT, "merge_test_durations_for_parity")
    assert discover_test_files(REPO_ROOT) == tuple(
        script.discover_test_files(REPO_ROOT)
    )


# =============================================================================
# supplement
# =============================================================================


def test_supplement_adds_unrecorded_files_and_leaves_every_existing_entry() -> None:
    record = {"tests/a.py": 604.0, "tests/b.py": 0.5}
    result = HandlerTestDurationsRefresh().handle(
        ModelTestDurationsRefreshRequest(
            operation=EnumTestDurationsOperation.SUPPLEMENT,
            test_files=("tests/a.py", "tests/b.py", "tests/c.py", "tests/d.py"),
            record=record,
            refresh_at=2,
            measured={
                "tests/c.py::test_one": 3.0,
                "tests/c.py::test_two": 0.456,
                "tests/a.py::test_other": 1.0,
            },
            recorded_on="2026-11-03",
            runner="r",
            base_sha="abcdef0123456789",
        )
    )
    assert result.updated_record is not None
    assert result.updated_record["tests/a.py"] == 604.0
    assert result.updated_record["tests/b.py"] == 0.5
    assert result.updated_record["tests/c.py"] == 3.46
    assert result.updated_record["tests/d.py"] == UNMEASURED_FILE_SECONDS
    assert result.measured_files == ("tests/c.py",)
    assert result.unmeasured_files == ("tests/d.py",)
    assert list(result.updated_record) == sorted(result.updated_record)
    assert result.provenance_key == "supplement_dev_refresh_abcdef012345"
    assert result.provenance_entry is not None
    assert result.provenance_entry.files_added == 2
    assert result.provenance_entry.files_without_measurement == 1
    # the input record is not mutated
    assert record == {"tests/a.py": 604.0, "tests/b.py": 0.5}


def test_supplement_cli_records_the_files_writes_provenance_and_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    at = contract_refresh_at()
    record = _repo(tmp_path, recorded=10, unrecorded=at)
    durations = tmp_path / "refresh.test_durations"
    durations.write_text(
        json.dumps(
            {
                "tests/test_f010.py::test_x": 1.25,
                "tests/test_f010.py::test_y": 0.25,
                "tests/test_f011.py::test_x": 2.0,
            }
        ),
        encoding="utf-8",
    )
    assert _run(*_supplement_args(durations, tmp_path), root=tmp_path) == 0

    after = json.loads(
        (tmp_path / "config" / "test_file_durations.json").read_text(encoding="utf-8")
    )
    assert {name: after[name] for name in record} == record
    assert after["tests/test_f010.py"] == 1.5
    assert after["tests/test_f011.py"] == 2.0
    assert after["tests/test_f012.py"] == UNMEASURED_FILE_SECONDS
    assert len(after) == 10 + at

    provenance = json.loads(
        (tmp_path / "config" / "test_file_durations_provenance.json").read_text(
            encoding="utf-8"
        )
    )
    entry = provenance["supplement_dev_refresh_abcdef012345"]
    assert entry["files_added"] == at
    assert entry["files_without_measurement"] == at - 2
    assert entry["base_sha"] == "abcdef0123456789abcdef0123456789abcdef01"
    assert provenance["ticket"] == "OMN-19684"

    # the refreshed record clears the freshness bar: nothing is unrecorded now
    capsys.readouterr()
    assert _run("status", root=tmp_path) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["unrecorded"] == 0
    assert status["due"] is False

    # a second run finds nothing to add and changes no byte
    before = {
        path.name: path.read_bytes() for path in (tmp_path / "config").glob("*.json")
    }
    assert _run(*_supplement_args(durations, tmp_path), root=tmp_path) == 0
    assert "no unrecorded test files" in capsys.readouterr().out
    assert before == {
        path.name: path.read_bytes() for path in (tmp_path / "config").glob("*.json")
    }


def test_supplement_cli_refuses_a_missing_durations_file(tmp_path: Path) -> None:
    _repo(tmp_path, recorded=2, unrecorded=contract_refresh_at())
    with pytest.raises(FileNotFoundError):
        _run(*_supplement_args(tmp_path / "absent", tmp_path), root=tmp_path)


# =============================================================================
# the cap and the threshold
# =============================================================================


def test_threshold_leaves_the_refresh_pr_room_below_the_unchanged_cap() -> None:
    freshness = _load(FRESHNESS_TEST, "freshness_for_refresh_threshold")
    # the cap is 50 and is never raised to make a change land (OMN-22389)
    assert freshness.MAX_UNRECORDED_FILES == 50
    # the refresh opens at under half the cap, so the PR has at least 25 new
    # files of room to land before any open PR goes red
    assert 0 < contract_refresh_at() <= freshness.MAX_UNRECORDED_FILES // 2


# =============================================================================
# workflow
# =============================================================================


def _workflow() -> dict[str, Any]:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _steps() -> list[dict[str, Any]]:
    steps = _workflow()["jobs"]["refresh"]["steps"]
    assert isinstance(steps, list)
    return steps


def test_workflow_runs_on_a_dev_push_and_by_hand_with_read_only_default() -> None:
    document = _workflow()
    triggers = document.get(True) or document.get("on")
    assert triggers["push"]["branches"] == ["dev"]
    assert "tests/**" in triggers["push"]["paths"]
    assert "workflow_dispatch" in triggers
    assert document["permissions"] == {"contents": "read"}
    assert document["jobs"]["refresh"]["runs-on"] == "ubuntu-latest"


def test_workflow_counts_first_and_does_the_rest_only_when_due() -> None:
    steps = _steps()
    names = [step.get("name", step.get("uses", "")) for step in steps]
    count_index = names.index("Count the unrecorded test files")
    count = steps[count_index]
    assert count["id"] == "status"
    assert f"{NODE_MODULE} status" in count["run"]
    # checkout, uv and the install come before the count; everything after is gated
    for step in steps[:count_index]:
        assert "if" not in step
    after = steps[count_index + 1 :]
    assert after
    for step in after:
        assert step.get("if") == "steps.status.outputs.due == 'true'", step.get("name")


def test_workflow_measures_the_listed_files_then_supplements_and_opens_one_pr() -> None:
    by_name = {step["name"]: step for step in _steps() if "name" in step}
    measure = by_name["Measure the unrecorded files once"]["run"]
    assert "--store-durations" in measure
    assert '-m "not kafka"' in measure
    assert "unrecorded.txt" in measure
    add = by_name["Add the files to the record"]["run"]
    assert f"{NODE_MODULE} supplement" in add
    pr = by_name["Open or update the refresh PR"]["run"]
    assert "gh pr create" in pr
    assert "gh pr edit" in pr
    assert "--state open" in pr
    # only the two data files are staged; a test, the cap or a workflow never is
    assert "git add config/test_file_durations.json" in pr
    assert "git add tests" not in pr
    assert "git add ." not in pr
    assert "git add -A" not in pr


def test_workflow_pull_request_uses_the_app_identity_and_one_fixed_branch() -> None:
    document = _workflow()
    mint = next(step for step in _steps() if step.get("id") == "app-token")
    assert mint["uses"].startswith("actions/create-github-app-token@")
    assert mint["with"]["permission-contents"] == "write"
    assert mint["with"]["permission-pull-requests"] == "write"
    assert document["env"]["REFRESH_BRANCH"].startswith("automation/")
    assert document["concurrency"]["cancel-in-progress"] is False


def test_workflow_passes_no_event_text_into_a_shell() -> None:
    # Values reach a script only through env: no run block interpolates a
    # ${{ }} expression.
    for step in _steps():
        script = step.get("run", "")
        assert "${{" not in script, step.get("name")


def test_workflow_pins_each_action_to_a_full_sha() -> None:
    for step in _steps():
        uses = step.get("uses")
        if uses is None:
            continue
        ref = uses.split("@", 1)[1].split()[0]
        assert len(ref) == 40, uses
