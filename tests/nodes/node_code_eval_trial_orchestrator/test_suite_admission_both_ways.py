# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24562 -- a suite task is kept only if, run the way a model is graded, its known-good answer passes
and an empty answer fails. Each case builds a real git repository and runs the real score handler over it
(the check is a pytest file placed over the worktree), so the verdicts are the grader's own."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
import yaml

from omnimarket.models.code_eval_trial.model_code_eval_run_request import (
    ModelCodeEvalCandidate,
)
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.eval_suite_census.model_suite_task import (
    ModelCodeEvalSuiteCheck,
    ModelCodeEvalSuiteTask,
)
from omnimarket.models.model_code_score import ModelCodeScoreOverlay
from omnimarket.nodes.node_code_contract_score_effect.models import (
    EnumCodeScoreVerdict,
)
from tests.nodes.node_code_eval_trial_orchestrator.suite_admission import (
    admit,
    static_refusal,
)
from tests.nodes.node_code_eval_trial_orchestrator.test_code_eval_suite_admission import (
    CONTRACTS,
    git,
)

pytestmark = pytest.mark.unit

REPO = "example"
ITEM = "dod-omn1001-unit"
CHECK = f"{sys.executable} -m pytest tests/test_a.py -q -p no:cacheprovider"
PASSING_TEST = "from pathlib import Path\n\n\ndef test_fixed() -> None:\n    assert 'return 1' in Path('src/a.py').read_text()\n"
FAILING_TEST = "def test_never() -> None:\n    assert False\n"


def contract(check_value: str, *, deploy: bool) -> str:
    base = yaml.safe_load(CONTRACTS.read_text(encoding="utf-8"))
    base["ticket_id"] = "OMN-1001"
    base["title"] = "Fix the greeting"
    base["summary"] = "The greeting returns one."
    first = copy.deepcopy(base["dod_evidence"][0])
    first.pop("execution_scope", None)
    first["id"] = ITEM
    first["checks"] = [
        {"check_type": "test_passes", "check_value": check_value, "cwd": "."}
    ]
    items = [first]
    if deploy:
        second = copy.deepcopy(first)
        second["id"] = "dod-omn1001-deploy"
        second["checks"] = [
            {"check_type": "command", "check_value": "ssh lab-host true", "cwd": "."}
        ]
        items.append(second)
    base["dod_evidence"] = items
    return yaml.safe_dump(base)


def build(
    tmp_path: Path,
    *,
    test_text: str,
    deploy: bool = False,
    other_contract: bool = False,
) -> tuple[Path, ModelCodeEvalCandidate, ModelCodeEvalSuiteTask]:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("def f():\n    return 0\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "base")
    (root / "src" / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text(test_text, encoding="utf-8")
    (root / "contracts").mkdir()
    (root / "contracts" / "OMN-1001.yaml").write_text(
        contract(CHECK, deploy=deploy), encoding="utf-8"
    )
    if other_contract:
        (root / "contracts" / "OMN-1002.yaml").write_text("other\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "change")
    merge = git(root, "rev-parse", "HEAD")
    candidate = ModelCodeEvalCandidate(
        repository=REPO, merge_commit=merge, contract_id="OMN-1001"
    )
    task = ModelCodeEvalSuiteTask(
        repository=REPO,
        merge_commit=merge,
        base_commit=git(root, "rev-parse", f"{merge}^1"),
        contract_id="OMN-1001",
        subject="fix the greeting",
        merged_at="2026-11-01T00:00:00Z",
        files_changed=2,
        lines_changed=4,
        band=EnumSizeBand.S,
        checks=(
            ModelCodeEvalSuiteCheck(
                item_id=ITEM, check_type="test_passes", check_value=CHECK
            ),
        ),
        check_files=("tests/test_a.py",),
        held_paths=("src/a.py",),
        control=EnumCodeScoreVerdict.PASS,
    )
    return root, candidate, task


def overlay(root: Path, scratch: Path) -> ModelCodeScoreOverlay:
    return ModelCodeScoreOverlay(
        check_timeout_s=120,
        env_sync_timeout_s=60,
        runnable_check_types=("test_passes", "command"),
        env_sync_command="true",
        scratch_root=str(scratch),
        repo_roots={REPO: str(root)},
        protected_paths={REPO: ("contracts/",)},
    )


def test_a_task_whose_known_good_passes_and_empty_fails_is_admitted(
    tmp_path: Path,
) -> None:
    root, candidate, task = build(tmp_path, test_text=PASSING_TEST)
    row = admit(candidate, overlay(root, tmp_path / "scratch"), (task,))
    assert (row.known_good, row.empty, row.refused) == ("pass", "fail_check", None)
    assert row.admitted


def test_break_a_known_good_that_fails_its_own_check_is_refused(tmp_path: Path) -> None:
    root, candidate, task = build(tmp_path, test_text=FAILING_TEST)
    row = admit(candidate, overlay(root, tmp_path / "scratch"), (task,))
    assert row.refused == "known_good_fail_check"
    assert not row.admitted


def test_a_task_the_reader_gives_no_held_answer_is_refused_with_the_reason(
    tmp_path: Path,
) -> None:
    root, candidate, _task = build(tmp_path, test_text=PASSING_TEST, deploy=True)
    row = admit(candidate, overlay(root, tmp_path / "scratch"), ())
    assert row.refused == "reader:check_names_no_changed_path"


def test_a_merge_that_carries_another_contract_is_admitted_by_its_recorded_held_paths(
    tmp_path: Path,
) -> None:
    root, candidate, task = build(
        tmp_path, test_text=PASSING_TEST, deploy=True, other_contract=True
    )
    unrecorded = admit(candidate, overlay(root, tmp_path / "scratch"), ())
    assert unrecorded.refused is not None
    row = admit(candidate, overlay(root, tmp_path / "scratch"), (task,))
    assert (row.known_good, row.empty, row.refused) == ("pass", "fail_check", None)


@pytest.mark.parametrize(
    "check_value",
    [
        "gh api repos/x/y/contents/z",
        "curl -s http://localhost:8085/health",
        "ssh lab-host true",
        "kubectl get pods",
    ],
)
def test_a_check_that_reaches_the_network_or_a_live_host_is_dropped(
    check_value: str,
) -> None:
    assert static_refusal(
        [("command", check_value)], ("src/a.py",), ("contracts/",)
    ) == ("check_needs_outside:command")


def test_a_held_answer_that_writes_a_protected_path_is_dropped() -> None:
    refusal = static_refusal(
        [("test_passes", "pytest tests/test_a.py")],
        ("contracts/OMN-9.yaml",),
        ("contracts/",),
    )
    assert refusal == "answer_needs_protected_path"
    assert (
        static_refusal(
            [("test_passes", "pytest tests/test_a.py")], ("src/a.py",), ("contracts/",)
        )
        is None
    )
