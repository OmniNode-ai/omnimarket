# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19451 -- enforce the omnimarket delegation-health reusable caller.

The caller must always produce the CI Summary context and pin the infra owner
of the verdict sources and implementation. Replay infra's own fixtures in a
subprocess so its ``scripts`` and ``tests`` packages cannot collide with ours.
Missing callers, invalid pins, and unreadable upstream files fail loudly.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import cast
from urllib.request import urlopen

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_FILE = "delegation-health-check.yml"
CALLER_PATH = REPO_ROOT / ".github/workflows" / WORKFLOW_FILE
JOB_ID = "delegation-health-check"
CONTEXT = "delegation-health-check / Delegation Health Check"
REUSABLE_PATH = ".github/workflows/delegation-health-reusable.yml"
USES_PREFIX = f"OmniNode-ai/omnibase_infra/{REUSABLE_PATH}@"
SECRETS = ("ONEXBOT_OCC_APP_ID", "ONEXBOT_OCC_PRIVATE_KEY")
INFRA_PATHS = (
    "scripts/__init__.py",
    "scripts/ci/__init__.py",
    "scripts/ci/delegation_health_check.py",
    "scripts/ci/lab_pass_receipt.py",
    "scripts/runtime_change_classifier.py",
    "config/delegation_health_check.yaml",
    "tests/__init__.py",
    "tests/ci/__init__.py",
    "tests/ci/test_delegation_health_check_omn19451.py",
    REUSABLE_PATH,
)
VALIDATOR_PATH = ".github/actions/deploy-gate/validate_pr_deploy_required.py"
# Real omnimarket dev nightly failure, 2026-09-24T01:50:55Z.
RED_NIGHTLY_RUN = 35944700620
RED_NIGHTLY = {"delegation-regression-nightly.yml": RED_NIGHTLY_RUN}
RUNTIME_FILES = ["src/omnimarket/delegation/__init__.py"]
DOCS_FILES = ["docs/README.md"]

DRIVER = r"""
from __future__ import annotations

import contextlib
import importlib
import json
import sys
from datetime import datetime, tzinfo
from io import StringIO
from pathlib import Path

tree = Path(sys.argv[1])
scenario = json.loads(sys.argv[2])
sys.path.insert(0, str(tree))

import scripts.ci.delegation_health_check as dh
import tests.ci.test_delegation_health_check_omn19451 as infra_test


class ReplayShim:
    def setattr(self, dotted_path: str, value: object) -> None:
        module_name, attribute = dotted_path.rsplit(".", 1)
        setattr(importlib.import_module(module_name), attribute, value)


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        return infra_test.NOW


infra_test._replay(ReplayShim(), scenario["red"])
dh.datetime = FrozenDatetime
record_path = tree / "record.json"
record_path.unlink(missing_ok=True)
output = StringIO()
with contextlib.redirect_stdout(output):
    code = dh.main([
        "--repo-key", "omnimarket",
        "--changed-files", ",".join(scenario["changed_files"]),
        "--labels", ",".join(scenario["labels"]),
        "--runtime-validator",
        str(tree / ".github/actions/deploy-gate/validate_pr_deploy_required.py"),
        "--record-out", str(record_path),
        "--config", str(tree / "config/delegation_health_check.yaml"),
    ])
print(json.dumps({
    "code": code,
    "stdout": output.getvalue(),
    "record": json.loads(record_path.read_text(encoding="utf-8")),
}))
"""


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict), f"Expected a mapping, got {value!r}"
    assert all(isinstance(key, str) for key in value), value
    return cast("dict[str, object]", value)


def _workflow(path: Path) -> dict[object, object]:
    assert path.is_file(), f"Required workflow is missing: {path}"
    raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), f"Expected a workflow mapping: {path}"
    return cast("dict[object, object]", raw)


def _triggers(workflow: dict[object, object]) -> dict[str, object]:
    # PyYAML parses a bare `on:` key as the boolean True.
    return _mapping(workflow.get(True, workflow.get("on")))


def _caller_job() -> dict[str, object]:
    jobs = _mapping(_workflow(CALLER_PATH)["jobs"])
    assert set(jobs) == {JOB_ID}, jobs
    return _mapping(jobs[JOB_ID])


def _infra_pin() -> str:
    uses = _caller_job()["uses"]
    assert isinstance(uses, str), uses
    assert uses.startswith(USES_PREFIX), uses
    pin = uses.removeprefix(USES_PREFIX)
    assert re.fullmatch(r"[0-9a-f]{40}", pin), f"Floating infra ref: {pin!r}"
    return pin


def _download(tree: Path, repo: str, pin: str, relative_path: str) -> None:
    url = f"https://raw.githubusercontent.com/OmniNode-ai/{repo}/{pin}/{relative_path}"
    try:
        with urlopen(url, timeout=60) as response:
            content = response.read()
    except Exception as exc:
        raise AssertionError(f"Download failed: {url}: {exc}") from exc
    destination = tree / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)


@pytest.fixture(scope="module")
def infra_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Fetch the implementation and fixtures at the actual caller's pin."""
    pin = _infra_pin()
    tree = tmp_path_factory.mktemp("delegation-health-infra")
    for relative_path in INFRA_PATHS:
        _download(tree, "omnibase_infra", pin, relative_path)
    deploy_gate = (REPO_ROOT / ".github/workflows/deploy-gate.yml").read_text(
        encoding="utf-8"
    )
    pins = set(
        re.findall(
            r"OmniNode-ai/omniclaude/\.github/actions/deploy-gate@"
            r"([0-9a-f]{40})(?=[\s\"']|$)",
            deploy_gate,
        )
    )
    assert len(pins) == 1, f"Expected one immutable deploy-gate pin, got {pins}"
    _download(tree, "omniclaude", pins.pop(), VALIDATOR_PATH)
    return tree


def _run_check(
    tree: Path,
    *,
    red: dict[str, int],
    changed_files: list[str],
    labels: list[str],
) -> tuple[int, str, dict[str, object]]:
    scenario = json.dumps(
        {"red": red, "changed_files": changed_files, "labels": labels}
    )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, "-c", DRIVER, str(tree), scenario],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, (
        f"Replay driver failed ({completed.returncode}):\n"
        f"{completed.stdout}\n{completed.stderr}"
    )
    lines = completed.stdout.splitlines()
    assert lines, f"Replay driver returned no JSON: {completed.stderr}"
    result = _mapping(json.loads(lines[-1]))
    code, stdout, record = result["code"], result["stdout"], result["record"]
    assert isinstance(code, int), result
    assert not isinstance(code, bool), result
    assert isinstance(stdout, str), result
    admission = _mapping(record)
    assert admission["errors"] == [], admission
    assert admission["exit_code"] == code, admission
    return code, stdout, admission


# Section 1: caller shape, with no network reads.
def test_caller_is_one_unconditional_reusable_job() -> None:
    job = _caller_job()
    assert not {
        "name",
        "needs",
        "if",
        "continue-on-error",
        "runs-on",
        "steps",
    }.intersection(job), job
    uses = job.get("uses")
    assert isinstance(uses, str), uses
    assert uses.startswith(USES_PREFIX), uses
    assert job.get("with") == {"repo_key": "omnimarket"}


def test_caller_pins_an_immutable_infra_sha() -> None:
    _infra_pin()


def test_caller_runs_for_pr_labels_and_merge_queue_without_filters() -> None:
    triggers = _triggers(_workflow(CALLER_PATH))
    assert "merge_group" in triggers
    pull_request = _mapping(triggers["pull_request"])
    types = pull_request["types"]
    assert isinstance(types, list), types
    assert {"labeled", "unlabeled"} <= set(types)
    assert not {"paths", "paths-ignore", "branches"}.intersection(pull_request)


def test_caller_passes_both_app_secrets() -> None:
    secrets = _mapping(_caller_job()["secrets"])
    for name in SECRETS:
        assert secrets[name] == f"${{{{ secrets.{name} }}}}"


def test_caller_concurrency_separates_refs() -> None:
    concurrency = _mapping(_workflow(CALLER_PATH)["concurrency"])
    group = concurrency["group"]
    assert isinstance(group, str), group
    assert "github.ref" in group, group


def test_ci_summary_enforces_the_context_without_an_exemption() -> None:
    gate = importlib.import_module("scripts.ci.ci_summary_gate")
    assert CONTEXT in gate.EXPECTED_EXTERNAL_CONTEXTS
    gate_tests = importlib.import_module("tests.unit.scripts.ci.test_ci_summary_gate")
    assert (WORKFLOW_FILE, JOB_ID) not in gate_tests.EXEMPT_CONTEXTS


def test_omnimarket_does_not_fork_infra_sources() -> None:
    assert not (REPO_ROOT / "config/delegation_health_check.yaml").exists()
    assert not (REPO_ROOT / "scripts/ci/delegation_health_check.py").exists()


# Section 2: contracts of the pinned reusable.
def test_pinned_reusable_produces_the_registered_context(infra_tree: Path) -> None:
    jobs = _mapping(_workflow(infra_tree / REUSABLE_PATH)["jobs"])
    inner = _mapping(jobs["delegation-health"])
    assert inner["name"] == "Delegation Health Check"
    (caller_job_id,) = _mapping(_workflow(CALLER_PATH)["jobs"])
    assert f"{caller_job_id} / {inner['name']}" == CONTEXT


def test_pinned_reusable_has_exactly_the_repo_key_input(infra_tree: Path) -> None:
    call = _mapping(_triggers(_workflow(infra_tree / REUSABLE_PATH))["workflow_call"])
    assert set(_mapping(call["inputs"])) == {"repo_key"}


def test_caller_passes_every_required_reusable_secret(infra_tree: Path) -> None:
    call = _mapping(_triggers(_workflow(infra_tree / REUSABLE_PATH))["workflow_call"])
    declarations = _mapping(call["secrets"])
    passed = _mapping(_caller_job()["secrets"])
    for name, declaration in declarations.items():
        if _mapping(declaration).get("required") is True:
            assert passed.get(name) == f"${{{{ secrets.{name} }}}}"


def test_pinned_reusable_runs_the_script_from_its_workflow_sha(
    infra_tree: Path,
) -> None:
    text = (infra_tree / REUSABLE_PATH).read_text(encoding="utf-8")
    assert "github.job_workflow_sha" in text
    assert "scripts/ci/delegation_health_check.py" in text


def test_pinned_config_reads_nightly_and_customer_surface_verdicts(
    infra_tree: Path,
) -> None:
    config = _mapping(
        yaml.safe_load(
            (infra_tree / "config/delegation_health_check.yaml").read_text(
                encoding="utf-8"
            )
        )
    )
    sources = config["sources"]
    assert isinstance(sources, list), sources
    workflows = {_mapping(source)["workflow"] for source in sources}
    assert {
        "delegation-regression-nightly.yml",
        "m4-c17-customer-surface-verdict.yml",
    } <= workflows


# Section 3: replay the owner's fixtures through its CLI and real classifier.
def test_red_nightly_blocks_a_runtime_change_and_names_the_run(
    infra_tree: Path,
) -> None:
    code, stdout, record = _run_check(
        infra_tree, red=RED_NIGHTLY, changed_files=RUNTIME_FILES, labels=[]
    )
    assert record["runtime_affecting"] is True, record
    assert code == 1, stdout
    assert str(RED_NIGHTLY_RUN) in stdout
    assert "delegation-regression-nightly" in stdout


def test_red_nightly_does_not_block_a_docs_only_change(infra_tree: Path) -> None:
    code, stdout, record = _run_check(
        infra_tree, red=RED_NIGHTLY, changed_files=DOCS_FILES, labels=[]
    )
    assert record["runtime_affecting"] is False, record
    assert code == 0, stdout
    assert "not runtime-affecting" in stdout


def test_all_green_is_a_positive_control_for_runtime_changes(infra_tree: Path) -> None:
    code, stdout, record = _run_check(
        infra_tree, red={}, changed_files=RUNTIME_FILES, labels=[]
    )
    assert record["runtime_affecting"] is True, record
    assert code == 0, stdout
    assert record["red_runs"] == []
    # Prove this CLI actually blocks the same paths when the nightly is red.
    red_code, red_stdout, _ = _run_check(
        infra_tree, red=RED_NIGHTLY, changed_files=RUNTIME_FILES, labels=[]
    )
    assert red_code == 1, red_stdout


def test_ticketed_fix_forward_admits_and_records_the_red(infra_tree: Path) -> None:
    code, stdout, record = _run_check(
        infra_tree,
        red=RED_NIGHTLY,
        changed_files=RUNTIME_FILES,
        labels=["delegation-fix-forward:OMN-19451"],
    )
    assert code == 0, stdout
    assert record["admitted_by"] == ["OMN-19451"]
    assert record["red_runs"] == [
        {"source": "delegation-regression-nightly", "run_id": str(RED_NIGHTLY_RUN)}
    ]


def test_fix_forward_without_a_ticket_is_refused(infra_tree: Path) -> None:
    code, stdout, record = _run_check(
        infra_tree,
        red=RED_NIGHTLY,
        changed_files=RUNTIME_FILES,
        labels=["delegation-fix-forward"],
    )
    assert code == 1, stdout
    assert "ticket" in stdout
    assert record["admitted_by"] == []


def test_runtime_change_label_cannot_admit_a_red(infra_tree: Path) -> None:
    code, stdout, record = _run_check(
        infra_tree,
        red=RED_NIGHTLY,
        changed_files=RUNTIME_FILES,
        labels=["runtime_change"],
    )
    assert code == 1, stdout
    assert record["admitted_by"] == []
