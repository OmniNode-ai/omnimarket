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
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.unit]

_WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
WORKFLOW = _WORKFLOWS / "hostile-reviewer.yml"
OVERLAY_SUFFIX = "docker/lane-overlays/hostile-review-voters.yaml"
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
# The endpoint variables the overlay replaced; no workflow may name them at all.
_RETIRED_ENDPOINT_VARS = ("LLM_LOCAL_STUDIO_PLANNER_URL", "LLM_QWEN3_REVIEW_URL")

# Positive control: the lines this repository's workflow carried before the
# overlay. The ratchet must find every one of them.
_KNOWN_BAD = """\
      LLM_LOCAL_STUDIO_PLANNER_URL: "http://studio.lab.example:8131"
          LLM_LOCAL_STUDIO_PLANNER_URL: ${{ vars.LLM_LOCAL_STUDIO_PLANNER_URL }}
          REVIEW_MODEL_KEYS: "qwen3-review local-studio-planner"
            --model qwen3-review \\
            --model local-studio-planner \\
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


@pytest.mark.parametrize("name", _RETIRED_ENDPOINT_VARS)
def test_no_workflow_reads_a_retired_endpoint_variable(name: str) -> None:
    for path in sorted(_WORKFLOWS.glob("*.y*ml")):
        assert name not in path.read_text(encoding="utf-8"), path.name


def test_review_reads_the_overlay() -> None:
    assert OVERLAY_ARG in str(_step("Run adversarial review")["run"])


def test_preflight_resolves_targets_from_the_overlay() -> None:
    run = str(_step("Preflight")["run"])
    assert "load_review_voters" in run
    assert '"$REVIEW_VOTERS_OVERLAY"' in run


def test_overlay_path_is_the_canonical_overlay() -> None:
    parsed = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    env = parsed["jobs"]["hostile-review"]["env"]
    assert str(env["REVIEW_VOTERS_OVERLAY"]).endswith(OVERLAY_SUFFIX)


def test_retry_budget_reads_the_overlay() -> None:
    assert OVERLAY_ARG in str(_step("Validate retry-budget invariant")["run"])


def test_overlay_is_read_live_from_omnibase_infra_dev() -> None:
    """Unpinned on purpose: a pin would bring back the per-PR branch update."""
    run = str(_step("Fetch review voters overlay")["run"])
    assert "github.com/OmniNode-ai/omnibase_infra.git" in run
    assert "--branch dev" in run
    assert "sparse-checkout set docker/lane-overlays" in run


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
    assert "if" not in _step("Install omniintelligence dependencies")
