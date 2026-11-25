# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A task the committed suite already records is read by the suite's own checks and held paths.

The census records, per kept task, the check items that fail at the base and the paths the
held answer touches. A merged change whose ticket contract also binds a deploy-time check
(one that names no changed path) or that carried another ticket's contract file was excluded by
the reader although the suite keeps it: four of the six rebuilt tasks read as not admitted. The
reader now follows the recorded task and hands the recorded item ids to the score request.
"""

from __future__ import annotations

import copy
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.models.code_eval_trial.model_code_eval_reduce_io import (
    ModelScoreToSend,
)
from omnimarket.models.code_eval_trial.model_code_eval_run_request import (
    ModelCodeEvalCandidate,
)
from omnimarket.models.code_eval_trial.model_code_eval_task_material import (
    ModelCodeEvalTaskMaterial,
)
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.eval_suite_census.model_suite_task import (
    ModelCodeEvalSuiteCheck,
    ModelCodeEvalSuiteTask,
)
from omnimarket.nodes.node_code_contract_score_effect.models import (
    EnumCodeScoreVerdict,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.orchestration.builders import (
    build_score_request,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.orchestration.suite_reader import (
    GitCodeEvalSuiteReader,
)
from tests.nodes.node_code_eval_trial_orchestrator.orchestrator_support import (
    score_overlay,
)
from tests.nodes.node_code_eval_trial_orchestrator.test_code_eval_suite_admission import (
    CONTRACTS,
    REPO,
    git,
)

pytestmark = pytest.mark.unit

TEST_ITEM = "dod-omn1001-unit"
DEPLOY_ITEM = "dod-omn1001-deploy"


def two_item_contract() -> str:
    """One test item naming a changed file and one deploy item naming none."""
    base = yaml.safe_load(CONTRACTS.read_text(encoding="utf-8"))
    base["ticket_id"] = "OMN-1001"
    base["title"] = "Fix the greeting"
    base["summary"] = "The greeting returns the name."
    first = copy.deepcopy(base["dod_evidence"][0])
    first.pop("execution_scope", None)
    first["id"] = TEST_ITEM
    first["checks"] = [
        {
            "check_type": "test_passes",
            "check_value": "uv run pytest tests/test_a.py -q",
            "cwd": ".",
        }
    ]
    second = copy.deepcopy(first)
    second["id"] = DEPLOY_ITEM
    second["checks"] = [
        {
            "check_type": "command",
            "check_value": "gh api repos/example/repo/contents/src/a.py",
            "cwd": ".",
        }
    ]
    base["dod_evidence"] = [first, second]
    return yaml.safe_dump(base)


def build_repo(
    tmp_path: Path, *, other_contract: bool
) -> tuple[Path, ModelCodeEvalCandidate]:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    for path in ("src/a.py", "tests/test_a.py"):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text("base\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "base")
    for path in ("src/a.py", "tests/test_a.py"):
        (root / path).write_text(f"changed {path}\n", encoding="utf-8")
    (root / "contracts").mkdir(exist_ok=True)
    (root / "contracts" / "OMN-1001.yaml").write_text(
        two_item_contract(), encoding="utf-8"
    )
    if other_contract:
        (root / "contracts" / "OMN-1002.yaml").write_text("other\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "change")
    merge = git(root, "rev-parse", "HEAD")
    return root, ModelCodeEvalCandidate(
        repository=REPO, merge_commit=merge, contract_id="OMN-1001"
    )


def recorded(candidate: ModelCodeEvalCandidate, root: Path) -> ModelCodeEvalSuiteTask:
    return ModelCodeEvalSuiteTask(
        repository=REPO,
        merge_commit=candidate.merge_commit,
        base_commit=git(root, "rev-parse", f"{candidate.merge_commit}^1"),
        contract_id="OMN-1001",
        subject="fix the greeting",
        merged_at="2026-11-01T00:00:00Z",
        files_changed=2,
        lines_changed=4,
        band=EnumSizeBand.S,
        checks=(
            ModelCodeEvalSuiteCheck(
                item_id=TEST_ITEM,
                check_type="test_passes",
                check_value="uv run pytest tests/test_a.py -q",
            ),
        ),
        check_files=("tests/test_a.py",),
        held_paths=("src/a.py",),
        control=EnumCodeScoreVerdict.PASS,
    )


def reader(
    root: Path, suite: tuple[ModelCodeEvalSuiteTask, ...] = ()
) -> GitCodeEvalSuiteReader:
    overlay = score_overlay().model_copy(
        update={
            "repo_roots": {REPO: str(root)},
            "protected_paths": {REPO: ("contracts/",)},
        }
    )
    return GitCodeEvalSuiteReader(
        score_overlay=overlay, max_task_chars=20_000, recorded_tasks=suite
    )


def test_a_deploy_check_that_names_no_changed_path_excludes_an_unrecorded_task(
    tmp_path: Path,
) -> None:
    root, candidate = build_repo(tmp_path, other_contract=False)
    (material,) = reader(root).read((candidate,))
    assert material.excluded_reason == "check_names_no_changed_path"


def test_a_recorded_task_is_read_by_the_checks_the_suite_recorded(
    tmp_path: Path,
) -> None:
    root, candidate = build_repo(tmp_path, other_contract=False)
    (material,) = reader(root, (recorded(candidate, root),)).read((candidate,))
    assert material.excluded_reason is None
    assert material.check_item_ids == (TEST_ITEM,)
    assert [f.path for f in material.check_files] == ["tests/test_a.py"]
    assert material.writable_globs == ("src/a.py",)


def test_another_tickets_contract_in_the_merge_does_not_exclude_a_recorded_task(
    tmp_path: Path,
) -> None:
    root, candidate = build_repo(tmp_path, other_contract=True)
    (unrecorded,) = reader(root).read((candidate,))
    assert unrecorded.excluded_reason is not None
    (material,) = reader(root, (recorded(candidate, root),)).read((candidate,))
    assert material.excluded_reason is None
    assert material.writable_globs == ("src/a.py",)
    assert "OMN-1002" not in material.held_diff


def test_a_recorded_task_on_another_merge_commit_is_not_used(tmp_path: Path) -> None:
    root, candidate = build_repo(tmp_path, other_contract=False)
    stale = recorded(candidate, root).model_copy(
        update={"merge_commit": "0" * 40, "base_commit": "1" * 40}
    )
    (material,) = reader(root, (stale,)).read((candidate,))
    assert material.excluded_reason == "check_names_no_changed_path"


def test_the_score_request_carries_the_recorded_item_ids(tmp_path: Path) -> None:
    root, candidate = build_repo(tmp_path, other_contract=False)
    (material,) = reader(root, (recorded(candidate, root),)).read((candidate,))
    request = build_score_request(
        uuid4(),
        ModelScoreToSend(
            task_key=material.task_key,
            model_arm="",
            context_arm="off",
            try_ordinal=1,
        ),
        material,
        score_overlay(),
    )
    assert request.check_item_ids == (TEST_ITEM,)


def test_material_item_ids_default_to_every_item() -> None:
    assert ModelCodeEvalTaskMaterial.model_fields["check_item_ids"].default == ()
