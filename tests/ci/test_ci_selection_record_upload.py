# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Detect Changes job keeps each pull request's selection as a typed record.

Reads ``.github/workflows/ci.yml`` and runs the shell of the step that exposes the executed
selection, so the workflow text itself is what is tested: the flag comes from the repository
variable, the record is uploaded under a name that carries the pull request, run id and
attempt and is never overwritten, and the pytest shard matrix is built from the record's
``executed_selection`` and from no other entry.
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.adapter_test_selection_policy_file import load_policy
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_in_memory import (
    SourceInMemory,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_selection_record import (
    HandlerSelectionRecord,
    build_artifact_name,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_test_selection_selector import (
    HandlerTestSelectionSelector,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_record import (
    EnumFullSuiteReason,
    ModelSelectionRecordRequest,
    ModelTestSelection,
    ModelTestSelectionRecord,
)
from scripts.ci.detect_test_paths import compute_selection

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
UPLOAD_ACTION = (
    "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"  # v4.6.2
)
ADJACENCY_DEFAULT = REPO_ROOT / "scripts" / "ci" / "test_selection_adjacency.yaml"
# OMN-26446: a dev push is a merged tree and takes the full-suite matrix, flag or not.
FLAG_EXPRESSION = (
    "${{ (vars.ENABLE_SMART_TESTS == 'true' && "
    "!(github.event_name == 'push' && github.ref == 'refs/heads/dev')) "
    "&& 'on' || 'off' }}"
)
NARROWABLE = ["src/omnimarket/intelligence/client.py"]


def _detect_steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps: list[dict[str, Any]] = workflow["jobs"]["detect-changes"]["steps"]
    return steps


def _step(name_part: str) -> dict[str, Any]:
    matches = [s for s in _detect_steps() if name_part in str(s.get("name", ""))]
    assert len(matches) == 1, f"expected one step named like {name_part!r}"
    return matches[0]


def _narrowed_selection() -> ModelTestSelection:
    """A fixed narrowed stage-0 selection, so the flag tests do not walk the repository's import graph."""
    return ModelTestSelection(
        selected_paths=["tests/intelligence/"],
        split_count=2,
        is_full_suite=False,
        matrix=[1, 2],
    )


@functools.cache
def _stage0_selection(*, flag: bool = True) -> ModelTestSelection:
    return compute_selection(
        changed_files=NARROWABLE,
        adjacency_path=ADJACENCY_DEFAULT,
        ref_name="feature/branch",
        event_name="pull_request",
        feature_flag_enabled=flag,
    )


def _request(
    stage_selections: dict[int, ModelTestSelection], *, flag: bool
) -> ModelSelectionRecordRequest:
    return ModelSelectionRecordRequest(
        repo="OmniNode-ai/omnimarket",
        pr_number=7,
        head_sha="a" * 40,
        base_sha="b" * 40,
        checkout_sha="c" * 40,
        run_id=123456,
        run_attempt=1,
        changed_files=tuple(NARROWABLE),
        flag_enabled=flag,
        stage_selections=stage_selections,
        repo_root=str(REPO_ROOT),
        selector_version="1.0.0",
        policy_version="p" * 12,
    )


def _handler() -> HandlerSelectionRecord:
    policy = load_policy()
    selector = HandlerTestSelectionSelector(
        SourceInMemory({"tests/intelligence/test_a.py": 120.0}), policy
    )
    return HandlerSelectionRecord(selector, policy)


# -- the flag ----------------------------------------------------------------


def test_workflow_passes_the_flag_from_the_repository_variable() -> None:
    step = _step("Resolve test selection")
    assert step["env"]["FLAG"] == FLAG_EXPRESSION
    assert '--feature-flag "$FLAG"' in step["run"]
    assert 'FLAG: "on"' not in WORKFLOW.read_text(encoding="utf-8")


def test_flag_states_same_input() -> None:
    narrowed = _narrowed_selection()
    assert narrowed.is_full_suite is False
    entries = {0: narrowed}
    on = _handler().handle(_request(entries, flag=True))
    off = _handler().handle(_request(entries, flag=False))
    assert on.executed_selection.kind == "narrowed"
    assert on.executed_selection.selected_paths == narrowed.selected_paths
    assert on.executed_selection.split_count == narrowed.split_count
    assert off.executed_selection.kind == "full_suite"
    assert off.executed_selection.reason_code is EnumFullSuiteReason.FEATURE_FLAG_OFF
    assert off.executed_selection.selected_paths == ["tests/"]
    assert on.selection_by_stage == off.selection_by_stage
    assert on.selection_by_stage[0].selected_paths == narrowed.selected_paths


def test_flag_off_full_matrix() -> None:
    full_split_count = load_policy().full_split_count
    off = _handler().handle(_request({0: _narrowed_selection()}, flag=False))
    assert off.executed_selection.split_count == full_split_count
    detect_off = _stage0_selection(flag=False)
    assert detect_off.split_count == full_split_count
    assert detect_off.matrix == list(range(1, full_split_count + 1))


def _assert_executed_matches_decided(changed: list[str], ref: str) -> None:
    decided = compute_selection(
        changed_files=changed,
        adjacency_path=ADJACENCY_DEFAULT,
        ref_name=ref,
        event_name="pull_request",
        feature_flag_enabled=True,
    )
    record = _handler().handle(_request({0: decided}, flag=True))
    executed = record.executed_selection
    assert executed.selected_paths == decided.selected_paths
    assert executed.split_count == decided.split_count
    assert (executed.kind == "full_suite") is decided.is_full_suite
    assert executed.reason_code == decided.full_suite_reason


def test_stage_zero_flag_on_runs_what_the_existing_selector_decides() -> None:
    for changed, ref in (
        (["src/omnimarket/models/x.py"], "feature/branch"),
        (["pyproject.toml"], "feature/branch"),
        (NARROWABLE, "main"),
    ):
        _assert_executed_matches_decided(changed, ref)


def test_stage_zero_narrowed_input_matches_the_graph_selector() -> None:
    # Walks the repository's whole import graph (about a minute), so the Done gate's 30 s
    # per-check ceiling cannot hold it; hosted CI runs it.
    _assert_executed_matches_decided(NARROWABLE, "feature/branch")


# -- the upload ---------------------------------------------------------------


def test_artifact_name_carries_pull_request_run_id_and_attempt() -> None:
    first = build_artifact_name(7, 123456, 1)
    assert first == "test-selection-7-123456-1"
    assert build_artifact_name(7, 123456, 2) != first
    assert build_artifact_name(7, 123456, 1) == first


def test_workflow_uploads_the_record_under_that_name_and_never_overwrites() -> None:
    step = _step("Upload selection record")
    assert step["uses"] == UPLOAD_ACTION
    with_ = step["with"]
    assert with_["overwrite"] is False
    assert with_["retention-days"] == "${{ steps.record.outputs.retention_days }}"
    template = with_["name"]
    expected = build_artifact_name(7, 123456, 2)
    rendered = (
        template.replace("${{ github.event.pull_request.number || 0 }}", "7")
        .replace("${{ github.run_id }}", "123456")
        .replace("${{ github.run_attempt }}", "2")
    )
    assert rendered == expected, template


def test_record_retention_comes_from_the_policy_not_the_workflow() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    block = text[text.index("  detect-changes:") : text.index("\n  test:\n")]
    assert "retention-days: 14" not in block
    assert load_policy().record_retention_days == 14


# -- the matrix ---------------------------------------------------------------


def _record_with_differing_entries() -> ModelTestSelectionRecord:
    narrowed = _narrowed_selection()
    stage_one = ModelTestSelection(
        selected_paths=["tests/a/"], split_count=1, is_full_suite=False, matrix=[1]
    )
    stage_two = ModelTestSelection(
        selected_paths=["tests/b/"], split_count=2, is_full_suite=False, matrix=[1, 2]
    )
    stage_three = ModelTestSelection(
        selected_paths=["tests/c/"],
        split_count=3,
        is_full_suite=False,
        matrix=[1, 2, 3],
    )
    policy = load_policy().model_copy(update={"selection_stage": 1})
    selector = HandlerTestSelectionSelector(SourceInMemory({}), policy)
    record = HandlerSelectionRecord(selector, policy).handle(
        _request({0: narrowed, 1: stage_one, 2: stage_two, 3: stage_three}, flag=True)
    )
    return record


def _run_expose_step(
    tmp_path: Path, record: ModelTestSelectionRecord
) -> dict[str, str]:
    (tmp_path / "selection-record.json").write_text(record.model_dump_json())
    output = tmp_path / "github_output"
    output.touch()
    script = "set -e\n" + str(_step("Expose the executed selection")["run"])
    subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_OUTPUT": str(output), "PYTHONPATH": "src"},
        check=True,
    )
    return dict(
        line.split("=", 1) for line in output.read_text().splitlines() if "=" in line
    )


def test_matrix_is_built_from_executed_selection_only(tmp_path: Path) -> None:
    record = _record_with_differing_entries()
    assert record.selection_stage == 1
    outputs = _run_expose_step(tmp_path, record)
    assert json.loads(outputs["selected_paths"]) == ["tests/a/"]
    assert outputs["split_count"] == "1"
    assert json.loads(outputs["matrix"]) == [1]
    for other in (0, 2, 3):
        entry = record.selection_by_stage[other]
        assert (json.loads(outputs["selected_paths"]), outputs["split_count"]) != (
            entry.selected_paths,
            str(entry.split_count),
        )


def test_workflow_reads_only_the_executed_selection() -> None:
    run = str(_step("Expose the executed selection")["run"])
    assert "executed_selection" in run
    assert "selection_by_stage" not in run
    assert "selection_by_stage" not in json.dumps(_detect_steps())


def test_detect_changes_keeps_its_needs_and_outputs() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["detect-changes"]
    assert job["name"] == "Detect Changes"
    assert job["needs"] == [
        "repo-evidence-preflight",
        "zone-filter",
        "dev-tree-pass-probe",
    ]
    assert set(job["outputs"]) == {
        "is_full_suite",
        "full_suite_reason",
        "split_count",
        "matrix",
        "selected_paths",
        # OMN-26277: read by the one whole-tree collection job, which follows this job.
        "collection_gate",
    }
    assert workflow["jobs"]["test"]["needs"] == [
        "repo-evidence-preflight",
        "detect-changes",
    ]
