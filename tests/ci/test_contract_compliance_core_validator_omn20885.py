# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20885: ``Contract Compliance Check`` and ``contract-validation`` validate with core.

Both jobs used to read onex_change_control: the ci.yml job checked it out,
installed it and ran its ``validate-yaml`` over the change-control repository's
own ``contracts/OMN-*.yaml`` (never this repository's), and
``contract-validation.yml`` checked out its validators at a pinned sha behind an
auth preflight against that repository. The validator both relied on is
``omnibase_core``'s ``ModelTicketContract``, which from 0.47.39 also refuses the
two shapes ``validate-yaml`` refused and core used to admit: a ``binds_ac``
entry that is not a criterion label, and a ``binds_ac`` item whose check type
is ``command_exit_0``.

Both jobs now validate this repository's own contracts with the installed core
model, under unchanged job and context names. These tests run each step's own
Python body, extracted from the workflow files, against the repo contracts and
against planted ones.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
CI = WORKFLOWS / "ci.yml"
CONTRACT_VALIDATION = WORKFLOWS / "contract-validation.yml"

CCC_JOB_KEY = "contract-compliance"
CCC_JOB_NAME = "Contract Compliance Check"
CCC_STEP_NAME = "Validate contract YAML files"
CV_WORKFLOW_NAME = "Contract Validation"
CV_JOB_KEY = "contract-validation"
CV_STEP_NAME = "Run contract validation"

_CONTRACT = """\
schema_version: "1.0.0"
ticket_id: "OMN-1"
title: "planted"
dod_evidence:
  - id: "dod-omn1-ac1"
    description: "planted"
    source: "manual"
    checks:
      - check_type: "{check_type}"
        check_value: "uv run pytest tests -q"
    binds_ac: ["{label}"]
"""
_CLEAN = _CONTRACT.format(check_type="test_passes", label="AC1")
_NON_LABEL = _CONTRACT.format(check_type="test_passes", label="AC1 trailing text")
_COMMAND_EXIT_0 = _CONTRACT.format(check_type="command_exit_0", label="AC1")


def _workflow(path: Path) -> dict[str, object]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _job(path: Path, key: str) -> dict[str, object]:
    jobs = _workflow(path)["jobs"]
    assert isinstance(jobs, dict)
    job = jobs[key]
    assert isinstance(job, dict)
    return job


def _step(path: Path, key: str, name: str) -> dict[str, object]:
    steps = _job(path, key)["steps"]
    assert isinstance(steps, list)
    (step,) = [step for step in steps if step.get("name") == name]
    return step


def _step_python(path: Path, key: str, name: str) -> str:
    run = _step(path, key, name)["run"]
    assert isinstance(run, str)
    head, _, rest = run.partition("<<'PY'\n")
    assert "python" in head, run
    body, _, _ = rest.partition("\nPY")
    assert body.strip(), run
    return body


def _run(body: str, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run(
        [sys.executable, "-c", body, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _ccc(cwd: Path) -> subprocess.CompletedProcess[str]:
    return _run(_step_python(CI, CCC_JOB_KEY, CCC_STEP_NAME), cwd)


def _cv(cwd: Path, *paths: str) -> subprocess.CompletedProcess[str]:
    return _run(
        _step_python(CONTRACT_VALIDATION, CV_JOB_KEY, CV_STEP_NAME), cwd, *paths
    )


def _contracts(tmp_path: Path, files: dict[str, str]) -> Path:
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    for name, text in files.items():
        (contracts / name).write_text(text, encoding="utf-8")
    return tmp_path


# --- the job shape: no change-control read, names unchanged -------------------


def test_contract_compliance_job_reads_no_onex_change_control() -> None:
    job = _job(CI, CCC_JOB_KEY)
    assert job["name"] == CCC_JOB_NAME
    assert job["needs"] == "occ-preflight"
    rendered = yaml.safe_dump(job)
    assert "onex_change_control" not in rendered
    assert "validate-yaml" not in rendered
    run = _step(CI, CCC_JOB_KEY, CCC_STEP_NAME)["run"]
    assert "ModelTicketContract" in str(run)


def test_contract_validation_workflow_reads_no_onex_change_control() -> None:
    text = CONTRACT_VALIDATION.read_text(encoding="utf-8")
    assert "onex_change_control" not in text
    assert "validate-yaml" not in text
    assert _workflow(CONTRACT_VALIDATION)["name"] == CV_WORKFLOW_NAME
    assert _job(CONTRACT_VALIDATION, CV_JOB_KEY)["name"] == CV_JOB_KEY
    run = _step(CONTRACT_VALIDATION, CV_JOB_KEY, CV_STEP_NAME)["run"]
    assert "ModelTicketContract" in str(run)


def test_both_jobs_run_the_same_validation_body() -> None:
    assert _step_python(CI, CCC_JOB_KEY, CCC_STEP_NAME) == _step_python(
        CONTRACT_VALIDATION, CV_JOB_KEY, CV_STEP_NAME
    )


# --- Contract Compliance Check: every repo contract ---------------------------


def test_repo_contracts_pass_the_core_model() -> None:
    result = _ccc(REPO_ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    count = len(list((REPO_ROOT / "contracts").glob("OMN-*.yaml")))
    assert count > 0
    assert f"{count} ticket contract(s)" in result.stdout


def test_clean_planted_contract_passes(tmp_path: Path) -> None:
    result = _ccc(_contracts(tmp_path, {"OMN-1.yaml": _CLEAN}))
    assert result.returncode == 0, result.stdout + result.stderr


def test_non_label_binds_ac_entry_fails(tmp_path: Path) -> None:
    root = _contracts(tmp_path, {"OMN-1.yaml": _CLEAN, "OMN-2.yaml": _NON_LABEL})
    result = _ccc(root)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "contracts/OMN-2.yaml" in result.stdout
    assert "DOD_EVIDENCE_BINDS_AC_LABEL" in result.stdout
    assert "contracts/OMN-1.yaml" not in result.stdout


def test_command_exit_0_binds_ac_item_fails(tmp_path: Path) -> None:
    root = _contracts(tmp_path, {"OMN-1.yaml": _CLEAN, "OMN-3.yaml": _COMMAND_EXIT_0})
    result = _ccc(root)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "contracts/OMN-3.yaml" in result.stdout
    assert "DOD_EVIDENCE_BINDS_AC_CHECK_TYPE" in result.stdout
    assert "contracts/OMN-1.yaml" not in result.stdout


def test_contracts_dir_with_no_ticket_contract_fails(tmp_path: Path) -> None:
    result = _ccc(_contracts(tmp_path, {"README.md": "no contracts\n"}))
    assert result.returncode == 1, result.stdout + result.stderr


# --- contract-validation: the branch ticket's contract ------------------------


def test_contract_validation_passes_a_clean_contract(tmp_path: Path) -> None:
    root = _contracts(tmp_path, {"OMN-1.yaml": _CLEAN})
    result = _cv(root, "contracts/OMN-1.yaml")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        (_NON_LABEL, "DOD_EVIDENCE_BINDS_AC_LABEL"),
        (_COMMAND_EXIT_0, "DOD_EVIDENCE_BINDS_AC_CHECK_TYPE"),
    ],
    ids=["non-label-binds-ac", "command-exit-0-binds-ac"],
)
def test_contract_validation_refuses_the_two_shapes(
    tmp_path: Path, text: str, rule: str
) -> None:
    root = _contracts(tmp_path, {"OMN-2.yaml": text})
    result = _cv(root, "contracts/OMN-2.yaml")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "contracts/OMN-2.yaml" in result.stdout
    assert rule in result.stdout


def test_contract_validation_validates_only_the_named_contract(tmp_path: Path) -> None:
    root = _contracts(tmp_path, {"OMN-1.yaml": _CLEAN, "OMN-2.yaml": _NON_LABEL})
    result = _cv(root, "contracts/OMN-1.yaml")
    assert result.returncode == 0, result.stdout + result.stderr
