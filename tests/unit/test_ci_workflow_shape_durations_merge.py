# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CI wiring for the pytest-split durations cache (OMN-19684).

The `test` job's shard balancer (`--splits N --group G`) reads
`.test_durations` to size shards by recorded per-test time. Before this
ticket, the cache-save step only ran on ``matrix.split == 1``, so 19 of the
20 full-suite shards' recorded durations were discarded every run and the
balancer only ever saw one shard's worth of data. Shard suite times spread
128-441s (3.4x) on a full-suite PR run (run 36186846950) as a result.

The fix: every full-suite shard uploads its own per-split durations file as
an artifact, and a downstream `merge-test-durations` job (mirroring the
existing per-shard coverage-artifact pattern at the `coverage-sweep-gate`
job) downloads all of them, merges the duration maps, and saves ONE cache
entry from the merged result.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

_OLD_SPLIT1_ONLY_STEP = "Save test durations (full suite only, split 1)"
_UPLOAD_STEP_NAME = "Upload test durations artifact (OMN-19684)"
_MERGE_JOB_KEY = "merge-test-durations"
_MERGE_SCRIPT = "scripts/ci/merge_test_durations.py"


def _load_workflow() -> dict[str, Any]:
    document = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _job(workflow: dict[str, Any], job_key: str) -> dict[str, Any]:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert job_key in jobs, f"expected job {job_key!r} in ci.yml"
    job = jobs[job_key]
    assert isinstance(job, dict)
    return job


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job["steps"]
    assert isinstance(steps, list)
    return steps


def test_split1_only_durations_save_step_is_gone() -> None:
    """The old single-shard cache-save step must not still be the save path."""
    workflow = _load_workflow()
    test_job = _job(workflow, "test")
    names = [step.get("name") for step in _steps(test_job)]
    assert _OLD_SPLIT1_ONLY_STEP not in names, (
        "the durations cache must no longer be saved from matrix.split == 1 "
        "alone -- it discards the other 19 shards' recorded durations"
    )


def test_every_full_suite_shard_uploads_its_own_durations_artifact() -> None:
    """Every shard (not just split 1) must upload its recorded durations."""
    workflow = _load_workflow()
    test_job = _job(workflow, "test")
    steps = _steps(test_job)
    upload_steps = [step for step in steps if step.get("name") == _UPLOAD_STEP_NAME]
    assert len(upload_steps) == 1, (
        f"expected exactly one {_UPLOAD_STEP_NAME!r} step in the test job"
    )
    upload = upload_steps[0]

    # Must run for every shard on a full-suite run, not gated to split 1.
    condition = str(upload.get("if", ""))
    assert "is_full_suite" in condition
    assert "matrix.split == 1" not in condition, (
        "the upload must not be limited to one shard"
    )

    with_block = upload.get("with", {})
    artifact_name = str(with_block.get("name", ""))
    artifact_path = str(with_block.get("path", ""))
    # Each shard's artifact name and durations file must be split-scoped so
    # a merge-multiple download does not overwrite one shard with another.
    assert "${{ matrix.split }}" in artifact_name
    assert "${{ matrix.split }}" in artifact_path

    pytest_step = next(
        step for step in steps if step.get("name") == "Run pytest (full suite)"
    )
    run = str(pytest_step.get("run", ""))
    assert "--store-durations" in run
    assert artifact_path.split("/")[-1] in run or artifact_path in run, (
        "the pytest step must write durations to the same path the upload "
        "step later reads"
    )


def test_merge_job_combines_every_shard_before_the_single_cache_save() -> None:
    """A downstream job merges every shard's durations into one cache entry."""
    workflow = _load_workflow()
    merge_job = _job(workflow, _MERGE_JOB_KEY)

    needs = merge_job.get("needs")
    needs_list = needs if isinstance(needs, list) else [needs]
    assert "test" in needs_list

    steps = _steps(merge_job)
    download_steps = [
        step for step in steps if "download-artifact" in str(step.get("uses", ""))
    ]
    assert len(download_steps) == 1
    download_with = download_steps[0].get("with", {})
    assert "test-durations-shard-" in str(download_with.get("pattern", ""))
    assert download_with.get("merge-multiple") is True

    merge_steps = [step for step in steps if _MERGE_SCRIPT in str(step.get("run", ""))]
    assert len(merge_steps) == 1, f"expected a step invoking {_MERGE_SCRIPT}"

    save_steps = [step for step in steps if "cache/save" in str(step.get("uses", ""))]
    assert len(save_steps) == 1, "expected exactly one cache/save step in the merge job"
    save_with = save_steps[0].get("with", {})
    assert save_with.get("key") == "test-durations-${{ github.sha }}"

    # The merge step must run before the cache is saved.
    assert steps.index(merge_steps[0]) < steps.index(save_steps[0])

    # The merge job's own condition must still require the full suite ran.
    condition = str(merge_job.get("if", ""))
    assert "is_full_suite" in condition


def test_merge_script_exists_and_merges_disjoint_shard_duration_maps(
    tmp_path: Path,
) -> None:
    """`merge_test_durations.py` unions per-shard JSON duration maps."""
    import json
    import subprocess
    import sys

    script = REPO_ROOT / _MERGE_SCRIPT
    assert script.is_file(), f"expected {_MERGE_SCRIPT} to exist"

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    (artifacts_dir / ".test_durations.1").write_text(
        json.dumps({"tests/unit/test_a.py::test_one": 0.5})
    )
    (artifacts_dir / ".test_durations.2").write_text(
        json.dumps({"tests/unit/test_b.py::test_two": 1.25})
    )
    output = tmp_path / ".test_durations"

    subprocess.run(
        [
            sys.executable,
            str(script),
            "--artifacts-dir",
            str(artifacts_dir),
            "--output",
            str(output),
        ],
        check=True,
        cwd=REPO_ROOT,
    )

    merged = json.loads(output.read_text())
    assert merged == {
        "tests/unit/test_a.py::test_one": 0.5,
        "tests/unit/test_b.py::test_two": 1.25,
    }
