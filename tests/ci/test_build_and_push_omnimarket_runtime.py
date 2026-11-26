# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""build-and-push-omnimarket-runtime.yml builds market's layer FROM infra's clean-main image.

The 2026-10-15 ruling is that the downstream image is built FROM infra's base image
and nothing in infra changes. Docker inherits the base image's clean-main label, so a
layer built from a dev-only omnimarket commit would pass the prod gate without a
lineage guard of its own. These tests pin what the workflow must do:

* it runs only by ``workflow_dispatch`` until the cloud owner answers;
* it refuses to label or push as clean-main unless the omnimarket commit is an
  ancestor of origin/main, proven by running the guard step's own script against a
  throwaway repository holding a main commit and a dev-only commit;
* it checks the base image's labels and the core and infra versions inside it, scans
  with Trivy, tags ``omninode-runtime:market-<sha12>-infra-<digest12>``, and writes a
  build manifest;
* it never writes a ``pinned-*`` tag: the cloud owner pins one digest per overlay.
"""

from __future__ import annotations

import copy
import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-and-push-omnimarket-runtime.yml"
GUARD_JOB = "lineage-guard"
BUILD_JOB = "build"
GUARD_STEP_ID = "lineage"
PROMOTION_LABEL = "com.omninode.promotion_class"
NON_MAIN_LABEL = "com.omninode.non_main_lineage"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
USES_REF = re.compile(r"^(?P<action>[^@\s]+)@(?P<ref>\S+)$")


def _load() -> dict[Any, Any]:
    loaded = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _triggers(doc: dict[Any, Any]) -> Any:
    # PyYAML reads the bare key ``on`` as the boolean True.
    return doc["on"] if "on" in doc else doc[True]


def _steps(doc: dict[Any, Any], job: str) -> list[dict[str, Any]]:
    steps = doc["jobs"][job]["steps"]
    assert isinstance(steps, list)
    return steps


def _all_steps(doc: dict[Any, Any]) -> list[dict[str, Any]]:
    return [step for job in doc["jobs"].values() for step in job["steps"]]


def _executable_text() -> str:
    """The workflow text with comment lines removed."""
    return "\n".join(
        line
        for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def _guard_step(doc: dict[Any, Any]) -> dict[str, Any] | None:
    steps: list[dict[str, Any]] = doc["jobs"].get(GUARD_JOB, {}).get("steps", [])
    for step in steps:
        if step.get("id") == GUARD_STEP_ID:
            return step
    return None


def lineage_guard_findings(doc: dict[Any, Any]) -> list[str]:
    """Every way the lineage guard is missing or not wired to the build, empty when sound."""
    findings: list[str] = []
    guard = _guard_step(doc)
    if guard is None:
        findings.append(f"no step with id {GUARD_STEP_ID!r} in job {GUARD_JOB!r}")
    else:
        script = str(guard.get("run", ""))
        if "merge-base --is-ancestor" not in script or "origin/main" not in script:
            findings.append("guard step does not test ancestry of origin/main")
    build = doc["jobs"].get(BUILD_JOB, {})
    if GUARD_JOB not in _as_list(build.get("needs")):
        findings.append(f"job {BUILD_JOB!r} does not need {GUARD_JOB!r}")
    build_text = yaml.safe_dump(build)
    for label in (PROMOTION_LABEL, NON_MAIN_LABEL):
        assignments = re.findall(
            rf"--label\s+{re.escape(label)}=(\$\{{\{{[^}}]*\}}\}}|\S+)", build_text
        )
        if not assignments:
            findings.append(f"build never sets {label}")
        for value in assignments:
            if f"needs.{GUARD_JOB}.outputs." not in value:
                findings.append(f"{label} is set from {value!r}, not the guard output")
    return findings


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


_GIT_LOCATION_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


def scrub_git_location_env(env: Mapping[str, str]) -> dict[str, str]:
    """Drop the git location variables before shelling out to git (OMN-14891)."""
    scrubbed = {k: v for k, v in env.items() if k not in _GIT_LOCATION_ENV}
    scrubbed["GIT_CONFIG_NOSYSTEM"] = "1"
    return scrubbed


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(
            {
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.invalid",
            }
        ),
    )
    return done.stdout.strip()


@pytest.fixture
def repo_with_main_and_dev(tmp_path: Path) -> dict[str, str | Path]:
    """A repository whose origin/main holds one commit and whose dev holds one more."""
    repo = tmp_path / "work"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("main\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "on main")
    main_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", main_sha)
    _git(repo, "checkout", "-q", "-b", "dev")
    (repo / "b.txt").write_text("dev only\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "dev only")
    return {"repo": repo, "main": main_sha, "dev": _git(repo, "rev-parse", "HEAD")}


def _run_guard(
    repo: Path, sha: str, tmp_path: Path
) -> tuple[subprocess.CompletedProcess[str], str]:
    guard = _guard_step(_load())
    assert guard is not None, "the workflow has no lineage guard step"
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    env = {
        **scrub_git_location_env(os.environ),
        "OMNIMARKET_SHA": sha,
        "GITHUB_OUTPUT": str(output),
    }
    done = subprocess.run(
        ["bash", "-c", str(guard["run"])],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return done, output.read_text(encoding="utf-8")


# --- LC2: dispatch only ---------------------------------------------------------


def test_workflow_file_exists() -> None:
    assert WORKFLOW.is_file()


def test_triggers_are_workflow_dispatch_only() -> None:
    triggers = _triggers(_load())
    assert isinstance(triggers, dict)
    assert set(triggers) == {"workflow_dispatch"}


def test_trigger_detector_positive_control() -> None:
    planted = copy.deepcopy(_load())
    planted["on"] = {"workflow_dispatch": {}, "push": {"branches": ["main"]}}
    planted.pop(True, None)
    assert set(_triggers(planted)) != {"workflow_dispatch"}


def test_dispatch_inputs_name_the_commit_digest_and_expected_versions() -> None:
    inputs = _triggers(_load())["workflow_dispatch"]["inputs"]
    for name in (
        "omnimarket_sha",
        "infra_base_digest",
        "expected_core_version",
        "expected_infra_version",
    ):
        assert inputs[name]["required"] is True, name
    assert inputs["push"]["default"] is False


# --- LC3: the lineage guard -----------------------------------------------------


def test_lineage_guard_is_present_and_wired_to_the_build() -> None:
    assert lineage_guard_findings(_load()) == []


def test_lineage_guard_check_fails_when_the_guard_step_is_removed() -> None:
    doc = copy.deepcopy(_load())
    doc["jobs"][GUARD_JOB]["steps"] = [
        step
        for step in doc["jobs"][GUARD_JOB]["steps"]
        if step.get("id") != GUARD_STEP_ID
    ]
    findings = lineage_guard_findings(doc)
    assert findings, "a workflow with the guard step removed must be reported"
    assert any("no step with id" in finding for finding in findings)


def test_lineage_guard_check_fails_when_the_label_is_hardcoded() -> None:
    doc = copy.deepcopy(_load())
    build = doc["jobs"][BUILD_JOB]
    for step in build["steps"]:
        if "run" in step:
            step["run"] = step["run"].replace(
                f"{PROMOTION_LABEL}=${{{{ needs.{GUARD_JOB}.outputs.promotion_class }}}}",
                f"{PROMOTION_LABEL}=clean-main",
            )
    assert any("not the guard output" in f for f in lineage_guard_findings(doc))


def test_main_ancestor_commit_is_labelled_clean_main(
    repo_with_main_and_dev: dict[str, str | Path], tmp_path: Path
) -> None:
    done, output = _run_guard(
        Path(repo_with_main_and_dev["repo"]),
        str(repo_with_main_and_dev["main"]),
        tmp_path,
    )
    assert done.returncode == 0, done.stderr
    assert "promotion_class=clean-main" in output
    assert "non_main_lineage=false" in output


def test_dev_only_commit_does_not_inherit_the_clean_main_label(
    repo_with_main_and_dev: dict[str, str | Path], tmp_path: Path
) -> None:
    done, output = _run_guard(
        Path(repo_with_main_and_dev["repo"]),
        str(repo_with_main_and_dev["dev"]),
        tmp_path,
    )
    assert done.returncode != 0, "a dev-only commit must be refused"
    assert "clean-main" not in output
    assert "promotion_class" not in output
    assert "::error::" in done.stdout + done.stderr


def test_unknown_or_malformed_sha_is_refused(
    repo_with_main_and_dev: dict[str, str | Path], tmp_path: Path
) -> None:
    for bad in ("0" * 40, "dev", "", "main; echo pwned"):
        done, output = _run_guard(Path(repo_with_main_and_dev["repo"]), bad, tmp_path)
        assert done.returncode != 0, bad
        assert "clean-main" not in output, bad


# --- build shape ----------------------------------------------------------------


def test_build_is_from_an_infra_digest_with_clean_main_labels() -> None:
    text = _executable_text()
    assert "INFRA_RUNTIME_IMAGE" in text
    assert "@${INFRA_BASE_DIGEST}" in text or "@${{ inputs.infra_base_digest }}" in text
    assert PROMOTION_LABEL in text
    assert NON_MAIN_LABEL in text
    assert "Dockerfile.runtime" in text
    assert "docker/Dockerfile.runtime" not in text


def test_versions_inside_the_image_are_checked() -> None:
    text = _executable_text()
    assert "expected_core_version" in text.lower() or "EXPECTED_CORE_VERSION" in text
    assert "EXPECTED_INFRA_VERSION" in text
    assert "omnibase-core" in text or "omnibase_core" in text
    assert "omnibase-infra" in text or "omnibase_infra" in text


def test_trivy_runs_and_fails_the_build_on_findings() -> None:
    scans = [
        step
        for step in _all_steps(_load())
        if str(step.get("uses", "")).startswith("aquasecurity/trivy-action@")
    ]
    assert scans, "no Trivy step"
    gating = [s for s in scans if str(s["with"].get("exit-code")) == "1"]
    assert gating, "no Trivy step fails the build"
    for step in gating:
        assert "CRITICAL" in step["with"]["severity"]
        assert str(step["with"].get("ignore-unfixed", "false")).lower() != "true"


def test_tag_shape_and_build_manifest() -> None:
    text = _executable_text()
    assert "omninode-runtime" in text
    assert "market-${SHA12}-infra-${DIGEST12}" in text or re.search(
        r"market-\$\{\w*SHA12\}-infra-\$\{\w*DIGEST12\}", text
    )
    uploads = [
        s
        for s in _all_steps(_load())
        if str(s.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert uploads, "the build manifest is not uploaded"
    assert "build-manifest.json" in text


def test_push_is_opt_in_and_gated_on_guard_and_scan() -> None:
    pushes = [
        s for s in _steps(_load(), BUILD_JOB) if "docker push" in str(s.get("run", ""))
    ]
    assert len(pushes) == 1
    condition = str(pushes[0].get("if", ""))
    assert "inputs.push" in condition
    assert "success()" in condition


# --- LC4: no pinned tag, no write outside the one tag ---------------------------


def test_workflow_never_writes_a_pinned_tag() -> None:
    text = _executable_text()
    assert "pinned-" not in text
    for line in text.splitlines():
        if re.search(r"docker\s+(push|tag)|--tag|tags:", line):
            assert "pinned" not in line, line


def test_pinned_tag_detector_positive_control() -> None:
    planted = (
        _executable_text()
        + '\n          docker push "${REPO}:pinned-onex-x-live-abcd1234"\n'
    )
    assert "pinned-" in planted


def test_no_latest_tag() -> None:
    assert ":latest" not in _executable_text()


# --- shape and hygiene ----------------------------------------------------------


def test_third_party_actions_are_pinned_to_a_full_sha() -> None:
    for step in _all_steps(_load()):
        uses = step.get("uses")
        if uses is None:
            continue
        match = USES_REF.match(str(uses))
        assert match, uses
        assert FULL_SHA.match(match["ref"]), f"{uses} is not pinned to a full sha"


def test_dispatch_inputs_never_reach_a_run_script_directly() -> None:
    for step in _all_steps(_load()):
        script = str(step.get("run", ""))
        assert "${{ inputs." not in script, step.get("name")
        assert "${{ github.event.inputs." not in script, step.get("name")


def test_public_runner_and_no_account_id_written_out() -> None:
    doc = _load()
    for name, job in doc["jobs"].items():
        assert job["runs-on"] == "ubuntu-latest", name
    assert not re.search(r"\b\d{12}\b", WORKFLOW.read_text(encoding="utf-8"))
    assert "vars.AWS_ACCOUNT_ID" in WORKFLOW.read_text(encoding="utf-8")


def test_permissions_default_to_read_only() -> None:
    doc = _load()
    assert doc["permissions"] == {"contents": "read"}
