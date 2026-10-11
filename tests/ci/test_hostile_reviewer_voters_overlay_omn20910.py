# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Hostile Reviewer reads its voters from the contract overlay (OMN-20910).

Operator RULING 2026-10-10T18:17:37Z: review-voter models are declared in a
contract overlay, never in repository variables or workflow files. Retiring one
endpoint on 2026-10-10 broke this gate on every pull request because the
second voter's URL lived in an Actions variable per repository and a
hard-coded port in this workflow. These tests hold the workflow to passing
the overlay and nothing else, and the ratchet below refuses any voter endpoint,
port, model flag or endpoint variable creeping back into a workflow file.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.unit]

_WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
WORKFLOW = _WORKFLOWS / "hostile-reviewer.yml"
OVERLAY_ARG = '--voters-overlay "$REVIEW_VOTERS_OVERLAY"'

# What a voter endpoint looks like when someone writes one into a workflow:
# a URL with an explicit port, an endpoint variable, a --model flag, or the
# preflight's old model-key list.
_VOTER_ENDPOINT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"https?://[A-Za-z0-9.\-]+:\d{2,5}"),
    re.compile(r"\bLLM_[A-Z0-9_]+_URL\b"),
    re.compile(r"vars\.LLM_"),
    re.compile(r"--model[\s=]"),
    re.compile(r"\bREVIEW_MODEL_KEYS\b"),
)
# Positive control: the lines this repository's workflow carried before the
# overlay. The ratchet must find every one of them.
_KNOWN_BAD = """\
      LLM_EXAMPLE_VOTER_B_URL: "http://voter-b.example.invalid:9002"
          LLM_EXAMPLE_VOTER_B_URL: ${{ vars.LLM_EXAMPLE_VOTER_B_URL }}
          REVIEW_MODEL_KEYS: "example-voter-a example-voter-b"
            --model example-voter-a \\
            --model example-voter-b \\
"""


def voter_endpoint_findings(text: str) -> list[str]:
    """Every line of ``text`` that names a voter endpoint, port or model flag."""
    findings: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        if any(p.search(line) for p in _VOTER_ENDPOINT_PATTERNS):
            findings.append(f"{number}: {line.strip()}")
    return findings


def _reviewer_workflows() -> list[Path]:
    return [
        path
        for path in sorted(_WORKFLOWS.glob("*.y*ml"))
        if "review_pairing" in path.read_text(encoding="utf-8")
    ]


def _steps() -> list[dict[str, Any]]:
    parsed = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = parsed["jobs"]["hostile-review"]["steps"]
    assert isinstance(steps, list)
    return steps


def _step(prefix: str) -> dict[str, Any]:
    matches = [s for s in _steps() if str(s.get("name", "")).startswith(prefix)]
    assert len(matches) == 1, f"expected one step named {prefix!r}"
    return matches[0]


def test_the_ratchet_finds_every_known_bad_line() -> None:
    """Positive control: a zero below is only evidence if this finds five."""
    assert len(voter_endpoint_findings(_KNOWN_BAD)) == 5


def test_reviewer_workflows_exist() -> None:
    assert WORKFLOW in _reviewer_workflows()


@pytest.mark.parametrize("path", _reviewer_workflows(), ids=lambda p: p.name)
def test_no_reviewer_workflow_names_a_voter_endpoint(path: Path) -> None:
    findings = voter_endpoint_findings(path.read_text(encoding="utf-8"))
    assert findings == [], (
        f"{path.name} names a review voter endpoint, port or model; voters are "
        f"declared only in the overlay (OMN-20910): {findings}"
    )


def test_no_workflow_reads_any_llm_endpoint_variable() -> None:
    """Every ``LLM_*_URL`` endpoint variable is retired, named or not.

    A generic pattern, so it holds for variables nobody remembers to list.
    """
    pattern = re.compile(r"\bLLM_[A-Z0-9_]+_URL\b")
    offenders = [
        path.name
        for path in sorted(_WORKFLOWS.glob("*.y*ml"))
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_review_reads_the_overlay() -> None:
    assert OVERLAY_ARG in str(_step("Run adversarial review")["run"])


def test_preflight_resolves_targets_from_the_overlay() -> None:
    run = str(_step("Preflight")["run"])
    assert "load_review_voters" in run
    assert '"$REVIEW_VOTERS_OVERLAY"' in run


def test_the_preflight_log_names_voters_by_id_only() -> None:
    """The Actions log of a public repository is public: no host, port, URL or
    model of a voter reaches it, or the PR comment built from ``error=``."""
    run = str(_step("Preflight")["run"])
    assert "describe()" not in run
    assert "${HOST}:${PORT}" not in run
    assert "${URL}" not in run
    assert 'print(f"VOTER|{voter.voter_id}")' in run


def test_retry_budget_reads_the_overlay() -> None:
    assert OVERLAY_ARG in str(_step("Validate retry-budget invariant")["run"])


def test_omniintelligence_is_pinned_to_a_full_sha() -> None:
    run = str(_step("Clone omniintelligence")["run"])
    assert re.search(r"clone_with_retry omniintelligence \S+ \S+ [0-9a-f]{40}\b", run)


def test_preflight_runs_in_the_installed_environment() -> None:
    """The overlay reader imports omniintelligence.review_pairing, which needs numpy.

    A preflight run from the bare clone (``uv run --no-project``) before the
    install died on ModuleNotFoundError, resolved no voter and reported DEGRADED
    on every run, so the install comes first and the preflight uses its
    environment.
    """
    names = [str(s.get("name", "")) for s in _steps()]
    install = names.index("Install omniintelligence dependencies")
    preflight = next(i for i, n in enumerate(names) if n.startswith("Preflight"))
    assert install < preflight
    run = str(_step("Preflight")["run"])
    assert "--no-project" not in run
    assert "uv run --no-sync python" in run
    install_step = _step("Install omniintelligence dependencies")
    assert "if" not in install_step
    # A failed install fails the job; no step can read a verdict without it.
    assert "continue-on-error" not in install_step


# OMN-20923 (operator RULING 2026-10-10T21:57:38Z): the roster is a deployment
# fact. No shipped repository carries it; the workflow fetches it from the
# private CI overlay the org variable OMNI_CI_OVERLAY_REPO names, and with the
# variable unset the neutral default is "no review voters configured".
OVERLAY_ENV = (
    "${{ github.workspace }}/../ci-overlay/config/onex/overlays/"
    "node_review_voters_overlay_compute/overlay.yaml"
)


def test_overlay_path_is_the_ci_overlay_outside_the_workspace() -> None:
    parsed = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    env = parsed["jobs"]["hostile-review"]["env"]
    assert env["REVIEW_VOTERS_OVERLAY"] == OVERLAY_ENV
    assert "../ci-overlay" in str(_step("Clean stale dependency clones")["run"])


def test_the_ci_overlay_repository_comes_from_an_org_variable() -> None:
    step = _step("Resolve the CI overlay")
    assert step["env"]["CI_OVERLAY_REPO"] == "${{ vars.OMNI_CI_OVERLAY_REPO }}"
    fetch = _step("Fetch the CI overlay")
    assert fetch["with"]["repository"] == (
        "${{ github.repository_owner }}/${{ steps.ci-overlay.outputs.name }}"
    )
    assert fetch["with"]["persist-credentials"] is False
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "lane-overlays" not in text
    assert "github.com/OmniNode-ai/omnibase_infra.git" not in text


def _run_resolve(repo_var: str, tmp_path: Path) -> tuple[int, str, str]:
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    proc = subprocess.run(
        ["bash", "-c", str(_step("Resolve the CI overlay")["run"])],
        env={
            "CI_OVERLAY_REPO": repo_var,
            "GITHUB_OUTPUT": str(output),
            "PATH": "/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, output.read_text(encoding="utf-8"), proc.stdout


def test_a_fork_with_no_ci_overlay_gets_the_neutral_default(tmp_path: Path) -> None:
    """Unset variable: the gate fails closed, naming "no review voters configured"."""
    rc, out, stdout = _run_resolve("", tmp_path)
    assert rc == 1
    assert "no review voters configured" in stdout
    assert "name=" not in out


def test_our_ci_resolves_the_named_overlay_repository(tmp_path: Path) -> None:
    rc, out, _ = _run_resolve("deploy-config", tmp_path)
    assert rc == 0
    assert "name=deploy-config" in out
    rc, _, _ = _run_resolve("owner/repo", tmp_path)
    assert rc == 1
