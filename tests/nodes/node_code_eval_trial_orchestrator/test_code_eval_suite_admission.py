# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23882 -- the run leg's suite reader admits a task by rule 3 and reads its material by rule 4,
over a real git repository built in the test (OMN-23627 AC16: "admit an `endpoint` check" and "leave
a test under a check's directory writable"). Exemplar: ``tests/nodes/node_code_contract_score_effect``
(a scratch repository per test)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.models.code_eval_trial.model_code_eval_run_request import (
    ModelCodeEvalCandidate,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.orchestration.suite_reader import (
    GitCodeEvalSuiteReader,
)
from tests.nodes.node_code_eval_trial_orchestrator.orchestrator_support import (
    score_overlay,
)

pytestmark = pytest.mark.unit

REPO = "example/repo"
CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "OMN-23882.yaml"


def git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    return done.stdout.strip()


def contract_yaml(check_type: str, check_value: str) -> str:
    base = yaml.safe_load(CONTRACTS.read_text(encoding="utf-8"))
    base["ticket_id"] = "OMN-1001"
    base["title"] = "Fix the greeting"
    base["summary"] = "The greeting returns the name."
    for item in base["dod_evidence"]:
        item["checks"] = [
            {"check_type": check_type, "check_value": check_value, "cwd": "."}
        ]
        item.pop("execution_scope", None)
    return yaml.safe_dump(base)


def build_repo(
    tmp_path: Path,
    *,
    check_type: str = "test_passes",
    check_value: str = "uv run pytest tests/test_a.py -q",
    changed: tuple[str, ...] = ("src/a.py", "tests/test_a.py"),
    with_contract: bool = True,
) -> tuple[Path, ModelCodeEvalCandidate]:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    for path in ("src/a.py", "tests/test_a.py", "tests/test_b.py", "docs/x.md"):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text("base\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "base")
    for path in changed:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(f"changed {path}\n", encoding="utf-8")
    if with_contract:
        (root / "contracts").mkdir(exist_ok=True)
        (root / "contracts" / "OMN-1001.yaml").write_text(
            contract_yaml(check_type, check_value), encoding="utf-8"
        )
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "change")
    merge = git(root, "rev-parse", "HEAD")
    return root, ModelCodeEvalCandidate(
        repository=REPO, merge_commit=merge, contract_id="OMN-1001"
    )


def reader(
    root: Path, protected: tuple[str, ...] = ("contracts/",)
) -> GitCodeEvalSuiteReader:
    overlay = score_overlay().model_copy(
        update={
            "repo_roots": {REPO: str(root)},
            "protected_paths": {REPO: protected},
        }
    )
    return GitCodeEvalSuiteReader(score_overlay=overlay, max_task_chars=20_000)


def test_an_admitted_task_carries_its_base_check_files_and_held_answer(
    tmp_path: Path,
) -> None:
    root, candidate = build_repo(tmp_path)
    (material,) = reader(root).read((candidate,))
    assert material.excluded_reason is None
    assert material.base_commit == git(root, "rev-parse", f"{candidate.merge_commit}^1")
    assert [f.path for f in material.check_files] == ["tests/test_a.py"]
    assert material.writable_globs == ("src/a.py",)
    assert "src/a.py" in material.held_diff
    assert "tests/test_a.py" not in material.held_diff
    assert "contracts/" not in material.held_diff
    assert material.contract.path == "contracts/OMN-1001.yaml"
    assert material.band in {"S", "M", "L"}
    assert material.task_text.startswith("Fix the greeting")
    assert "uv run pytest" not in material.task_text, "a check value is never task text"


def test_the_same_candidate_reads_the_same_material(tmp_path: Path) -> None:
    root, candidate = build_repo(tmp_path)
    assert reader(root).read((candidate,)) == reader(root).read((candidate,))


def assert_endpoint_check_is_excluded(
    root: Path, candidate: ModelCodeEvalCandidate
) -> None:
    (material,) = reader(root).read((candidate,))
    assert material.excluded_reason == "check_not_runnable:endpoint"


def test_a_task_bound_to_an_endpoint_check_is_excluded(tmp_path: Path) -> None:
    root, candidate = build_repo(
        tmp_path, check_type="endpoint", check_value="http://localhost/health"
    )
    assert_endpoint_check_is_excluded(root, candidate)


def test_break_admit_an_endpoint_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, candidate = build_repo(
        tmp_path, check_type="endpoint", check_value="http://localhost/health"
    )
    monkeypatch.setattr(
        GitCodeEvalSuiteReader,
        "_names_a_changed_path",
        staticmethod(lambda _check_value, _changed: True),
    )
    admitted = score_overlay().model_copy(
        update={
            "repo_roots": {REPO: str(root)},
            "protected_paths": {REPO: ("contracts/",)},
            "runnable_check_types": ("test_passes", "command", "endpoint"),
        }
    )
    broken = GitCodeEvalSuiteReader(score_overlay=admitted, max_task_chars=20_000)

    def read_and_expect_the_exclusion() -> None:
        (material,) = broken.read((candidate,))
        assert material.excluded_reason == "check_not_runnable:endpoint"

    with pytest.raises(AssertionError):
        read_and_expect_the_exclusion()


def assert_no_test_under_a_check_directory_is_writable(
    root: Path, candidate: ModelCodeEvalCandidate, instance: GitCodeEvalSuiteReader
) -> None:
    (material,) = instance.read((candidate,))
    assert material.excluded_reason is None
    assert {f.path for f in material.check_files} == {
        "tests/test_a.py",
        "tests/test_b.py",
    }
    assert material.writable_globs == ("src/a.py",)
    assert not any(path.startswith("tests/") for path in material.writable_globs)


def directory_task(tmp_path: Path) -> tuple[Path, ModelCodeEvalCandidate]:
    return build_repo(
        tmp_path,
        check_value="uv run pytest tests/ -q",
        changed=("src/a.py", "tests/test_a.py", "tests/test_b.py"),
    )


def test_no_test_under_a_checks_directory_is_writable(tmp_path: Path) -> None:
    root, candidate = directory_task(tmp_path)
    assert_no_test_under_a_check_directory_is_writable(root, candidate, reader(root))


def test_break_leave_a_test_under_a_checks_directory_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, candidate = directory_task(tmp_path)
    monkeypatch.setattr(GitCodeEvalSuiteReader, "_check_files", lambda *_a, **_k: ())
    with pytest.raises(AssertionError):
        assert_no_test_under_a_check_directory_is_writable(
            root, candidate, reader(root)
        )


@pytest.mark.parametrize(
    ("name", "kwargs", "reason"),
    [
        ("no_contract", {"with_contract": False}, "contract_missing"),
        (
            "no_changed_path",
            {"check_value": "uv run pytest tests/test_b.py -q"},
            "check_names_no_changed_path",
        ),
        (
            "protected",
            {"changed": ("src/a.py", "tests/test_a.py", "contracts/other.yaml")},
            "protected_path",
        ),
        ("no_held_change", {"changed": ("tests/test_a.py",)}, "no_held_change"),
    ],
)
def test_each_exclusion_is_a_counted_reason_never_a_raise(
    tmp_path: Path, name: str, kwargs: dict, reason: str
) -> None:
    root, candidate = build_repo(tmp_path, **kwargs)
    (material,) = reader(root).read((candidate,))
    assert material.excluded_reason == reason, name


def test_a_repository_the_score_overlay_does_not_serve_is_excluded(
    tmp_path: Path,
) -> None:
    root, candidate = build_repo(tmp_path)
    other = candidate.model_copy(update={"repository": "example/other"})
    (material,) = reader(root).read((other,))
    assert material.excluded_reason == "repository_not_served"


def test_a_task_text_over_the_loop_limit_is_excluded(tmp_path: Path) -> None:
    root, candidate = build_repo(tmp_path)
    small = GitCodeEvalSuiteReader(
        score_overlay=reader(root)._score_overlay, max_task_chars=10
    )
    (material,) = small.read((candidate,))
    assert material.excluded_reason == "task_too_long"
