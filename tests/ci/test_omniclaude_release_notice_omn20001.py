# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20001 - an omniclaude release reaches omnimarket as a pin bump PR.

Operator ruling 2026-10-01T21:43:10Z (3): a producer change that extends a
cross-repo contract notifies its consumer, and the consumer's update is raised as
a PR that lands through the consumer's own checks. omniclaude's release workflow
dispatches ``omniclaude-release``; ``omniclaude-pin-bump.yml`` receives it and
moves the one omniclaude pin in ci.yml through node_omniclaude_pin_bump_compute.

Three properties, each with its own ``-k`` falsifier:

* ``bump_refuses``: the bump moves every omniclaude read to the new tag, and
  refuses a planted violation: a release candidate, a branch name, a version that
  is not ahead, disagreeing pins, a loose pin, no pin. A refusal changes nothing.
* ``workflow_receives_notice``: the workflow runs on the dispatch the producer
  sends, calls the node, and opens a PR against dev under the App identity.
* ``notice_wired``: this file runs in the lint job and a pre-commit hook.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_omniclaude_pin_bump_compute.handlers.handler_omniclaude_pin_bump import (
    HandlerOmniclaudePinBump,
)
from omnimarket.nodes.node_omniclaude_pin_bump_compute.models.model_omniclaude_pin_bump import (
    EnumPinBumpRefusal,
    ModelOmniclaudePinBumpRequest,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
NOTICE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "omniclaude-pin-bump.yml"
THIS_TEST = "tests/ci/test_omniclaude_release_notice_omn20001.py"
GUARD_STEP = "Omniclaude release notice (OMN-20001)"
GUARD_HOOK = "omniclaude-release-notice"
NODE_MODULE = "omnimarket.nodes.node_omniclaude_pin_bump_compute"

_PINNED = (
    "      - name: Clone omniclaude\n"
    "        run: git clone --depth=1 --branch v0.27.0 "
    "https://github.com/OmniNode-ai/omniclaude.git ../omniclaude\n"
    "      - name: Checkout omniclaude (census-only)\n"
    "        uses: actions/checkout@abc  # v4\n"
    "        with:\n"
    "          repository: OmniNode-ai/omniclaude\n"
    "          ref: v0.27.0\n"
    "          persist-credentials: false\n"
    "          path: .contract-graph-checkouts/omniclaude\n"
    "      - name: Checkout other\n"
    "        with:\n"
    "          repository: OmniNode-ai/omniintelligence\n"
    "          ref: 51e1cb356453ad325d76aea57ab371cf975df3a4\n"
)


def _bump(text: str, version: str) -> Any:
    return HandlerOmniclaudePinBump().handle(
        ModelOmniclaudePinBumpRequest(ci_text=text, new_version=version)
    )


# =============================================================================
# AC1: the bump moves the pin and refuses a planted violation
# =============================================================================


def test_bump_refuses_control_moves_every_read_and_only_omniclaude() -> None:
    result = _bump(_PINNED, "0.28.0")
    assert result.accepted
    assert result.current_tag == "v0.27.0"
    assert result.new_tag == "v0.28.0"
    assert result.updated_text.count("v0.28.0") == 2
    assert "v0.27.0" not in result.updated_text
    # the unrelated sibling pin on the next repository block is untouched
    assert "51e1cb356453ad325d76aea57ab371cf975df3a4" in result.updated_text


def test_bump_refuses_accepts_a_v_prefixed_version() -> None:
    assert _bump(_PINNED, "v0.28.0").new_tag == "v0.28.0"


@pytest.mark.parametrize("version", ["0.28.0rc1", "0.28.0-rc.1", "dev", "main", ""])
def test_bump_refuses_a_version_that_is_not_a_plain_release(version: str) -> None:
    result = _bump(_PINNED, version)
    assert not result.accepted
    assert result.refusal is EnumPinBumpRefusal.NOT_A_RELEASE_VERSION
    assert result.updated_text == ""


@pytest.mark.parametrize("version", ["0.27.0", "0.26.9", "0.2.0"])
def test_bump_refuses_a_version_that_is_not_ahead_of_the_pin(version: str) -> None:
    result = _bump(_PINNED, version)
    assert not result.accepted
    assert result.refusal is EnumPinBumpRefusal.NOT_FORWARD
    assert result.current_tag == "v0.27.0"


def test_bump_refuses_compares_numerically_not_as_text() -> None:
    assert _bump(_PINNED, "0.100.0").accepted
    assert _bump(_PINNED.replace("v0.27.0", "v0.100.0"), "0.28.0").refusal is (
        EnumPinBumpRefusal.NOT_FORWARD
    )


def test_bump_refuses_disagreeing_pins() -> None:
    split = _PINNED.replace("ref: v0.27.0", "ref: v0.26.0")
    result = _bump(split, "0.28.0")
    assert not result.accepted
    assert result.refusal is EnumPinBumpRefusal.PINS_DISAGREE


@pytest.mark.parametrize("ref", ["dev", "main", "0.27.0", "v0.27.0-rc1"])
def test_bump_refuses_a_read_that_is_not_at_a_release_tag(ref: str) -> None:
    loose = _PINNED.replace("ref: v0.27.0", f"ref: {ref}")
    result = _bump(loose, "0.28.0")
    assert not result.accepted
    assert result.refusal is EnumPinBumpRefusal.PIN_NOT_A_RELEASE_TAG


def test_bump_refuses_when_no_read_of_omniclaude_exists() -> None:
    result = _bump("jobs: {}\n", "0.28.0")
    assert not result.accepted
    assert result.refusal is EnumPinBumpRefusal.NO_PIN_FOUND


def test_bump_refuses_moves_every_read_in_the_live_ci_workflow() -> None:
    live = CI_WORKFLOW.read_text()
    result = _bump(live, "999.0.0")
    assert result.accepted, result.detail
    assert result.current_tag is not None
    assert result.updated_text.count("v999.0.0") >= 2
    assert result.current_tag not in {
        line.split("ref:")[-1].strip()
        for line in result.updated_text.splitlines()
        if "ref: v" in line
    }
    # moving the pin is the only change: line count is unchanged
    assert result.updated_text.count("\n") == live.count("\n")


def test_bump_refuses_cli_rewrites_the_file_on_accept_and_leaves_it_on_refuse(
    tmp_path: Path,
) -> None:
    target = tmp_path / "ci.yml"
    target.write_text(_PINNED)

    def run(version: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                NODE_MODULE,
                "--ci-yml",
                str(target),
                "--version",
                version,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )

    refused = run("0.28.0rc1")
    assert refused.returncode == 1
    assert json.loads(refused.stdout)["refusal"] == "not_a_release_version"
    assert target.read_text() == _PINNED

    accepted = run("0.28.0")
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout)["new_tag"] == "v0.28.0"
    assert target.read_text() == _PINNED.replace("v0.27.0", "v0.28.0")


# =============================================================================
# AC2: the workflow runs on the producer's dispatch and opens the PR
# =============================================================================


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def notice_workflow_violations(doc: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    triggers = doc.get(True) or doc.get("on") or {}
    dispatch = triggers.get("repository_dispatch") or {}
    if dispatch.get("types") != ["omniclaude-release"]:
        violations.append("does not run on repository_dispatch omniclaude-release")
    if "workflow_dispatch" not in triggers:
        violations.append("has no workflow_dispatch to replay a missed notice")
    jobs = doc.get("jobs") or {}
    steps = [s for job in jobs.values() for s in job.get("steps", [])]
    runs = "\n".join(str(s.get("run", "")) for s in steps)
    if NODE_MODULE not in runs or "--ci-yml .github/workflows/ci.yml" not in runs:
        violations.append("does not move the pin through the bump node")
    if "gh pr create" not in runs or "--base dev" not in runs:
        violations.append("does not open a PR against dev")
    uses = [str(s.get("uses", "")) for s in steps]
    if not any(u.startswith("actions/create-github-app-token@") for u in uses):
        violations.append("does not mint the onexbot-occ-writer App token")
    if "PAT" in json.dumps(doc):
        violations.append("reads a personal access token")
    for step in steps:
        if "${{" in str(step.get("run", "")):
            violations.append(
                f"step {step.get('name')!r} interpolates an expression into the script"
            )
    return violations


def test_workflow_receives_notice_holds_on_the_live_workflow() -> None:
    assert notice_workflow_violations(_load(NOTICE_WORKFLOW)) == []


def test_workflow_receives_notice_flags_a_missing_dispatch_trigger() -> None:
    doc = copy.deepcopy(_load(NOTICE_WORKFLOW))
    del doc[True]["repository_dispatch"]
    assert "does not run on repository_dispatch omniclaude-release" in (
        notice_workflow_violations(doc)
    )


def test_workflow_receives_notice_flags_a_workflow_that_skips_the_bump_node() -> None:
    doc = copy.deepcopy(_load(NOTICE_WORKFLOW))
    for job in doc["jobs"].values():
        for step in job["steps"]:
            if NODE_MODULE in str(step.get("run", "")):
                step["run"] = "sed -i s/v0.27.0/v9.9.9/ .github/workflows/ci.yml"
    assert "does not move the pin through the bump node" in (
        notice_workflow_violations(doc)
    )


def test_workflow_receives_notice_flags_an_expression_in_a_script() -> None:
    doc = copy.deepcopy(_load(NOTICE_WORKFLOW))
    doc["jobs"]["bump"]["steps"][-1]["run"] += (
        '\necho "${{ github.event.client_payload.version }}"'
    )
    assert any("interpolates" in v for v in notice_workflow_violations(doc))


# =============================================================================
# AC1/AC2: the check itself is wired
# =============================================================================


def _precommit_hook(hook_id: str) -> dict[str, Any]:
    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text())
    hooks = [
        hook
        for repo in config["repos"]
        for hook in repo.get("hooks", [])
        if hook.get("id") == hook_id
    ]
    assert len(hooks) == 1, f"expected one {hook_id!r} hook, found {len(hooks)}"
    return hooks[0]


def test_notice_wired_into_lint_and_precommit() -> None:
    steps = _load(CI_WORKFLOW)["jobs"]["lint"]["steps"]
    matches = [s for s in steps if s.get("name") == GUARD_STEP]
    assert len(matches) == 1
    assert THIS_TEST in str(matches[0]["run"])
    hook = _precommit_hook(GUARD_HOOK)
    assert THIS_TEST in hook["entry"]
    assert hook["stages"] == ["pre-commit"]
    assert hook["pass_filenames"] is False
    files = hook["files"]
    assert "omniclaude-pin-bump" in files
    assert "node_omniclaude_pin_bump_compute" in files
