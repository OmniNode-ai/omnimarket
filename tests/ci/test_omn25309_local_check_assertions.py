# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local Done-gate checks of the unimported-handler contracts can fail.

The hosted runner holds no pull request state, so a criterion about a PR comment,
the check-runs or mergeability cannot be bound to a test over the PR. Each such
criterion carries an unbound ``local_done_gate`` item whose command asserts the
fact. These tests run each of those commands with a stub ``onex`` on the path and
require it to pass on a PR that holds the fact and to fail on one that does not.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_ticket_contract_prerequisite_effect.handlers.handler_integration_proof import (
    check_integration_proof,
)
from omnimarket.nodes.node_ticket_contract_prerequisite_effect.models.model_integration_proof_request import (
    ModelIntegrationProofRequest,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DHC = "delegation-health-check / Delegation Health Check"
_RUNTIME_HELPER = "src/omnimarket/handlers/unimported_handler_baseline.py"


def _head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_REPO_ROOT,
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _item(contract: str, item_id: str) -> dict[str, object]:
    document = yaml.safe_load(
        (_REPO_ROOT / "contracts" / f"{contract}.yaml").read_text(encoding="utf-8")
    )
    for item in document["dod_evidence"]:
        if item["id"] == item_id:
            return dict(item)
    raise AssertionError(f"{contract}: no dod_evidence item {item_id}")


def _command(contract: str, item_id: str) -> str:
    item = _item(contract, item_id)
    assert item.get("execution_scope") == "local_done_gate"
    assert not item.get("binds_ac")
    checks = item["checks"]
    assert isinstance(checks, list)
    assert len(checks) == 1
    return str(checks[0]["check_value"])


def _run(command: str, tmp_path: Path, outputs: dict[str, object]) -> int:
    """Run a contract command with a stub ``onex`` that prints canned JSON."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "onex"
    stub.write_text(
        '#!/bin/sh\n# argv: scm <verb> <target>\ncat "$STUB_DIR/$2.json"\n',
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    for verb, payload in outputs.items():
        (bin_dir / f"{verb}.json").write_text(json.dumps(payload), encoding="utf-8")
    env = {
        **scrub_git_location_env(os.environ),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }
    env["STUB_DIR"] = str(bin_dir)
    return subprocess.run(
        ["bash", "-c", command],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    ).returncode


def _view(*bodies: str) -> dict[str, object]:
    return {"result": {"comments": [{"body": body} for body in bodies]}}


def _checks(head: str, red: list[str], pending: list[str] | None = None) -> dict:
    return {
        "result": {
            "head_sha": head,
            "red": red,
            "pending": pending or [],
            "class": "red" if red else "green",
        }
    }


_LAB_ITEMS = [
    ("OMN-24837", "dod-omn24837-lab-comment"),
    ("OMN-24794", "dod-omn24794-lab-comment-local"),
]
_FINDINGS_ITEM = ("OMN-24837", "dod-omn24837-non-blocking-findings")
_CHECKS_ITEMS = [
    ("OMN-24837", "dod-omn24837-required-checks-local"),
    ("OMN-24794", "dod-omn24794-required-checks-local"),
]


@pytest.mark.unit
@pytest.mark.parametrize(("contract", "item_id"), _LAB_ITEMS)
def test_contract_lab_comment_check_passes_only_with_a_lab_comment_at_head(
    tmp_path: Path, contract: str, item_id: str
) -> None:
    command = _command(contract, item_id)
    head = _head()
    holds = _view("unrelated", f"Lab: head={head} host=h051 exit=0")
    assert _run(command, tmp_path, {"pr-view": holds}) == 0
    assert _run(command, tmp_path, {"pr-view": _view()}) != 0
    assert _run(command, tmp_path, {"pr-view": _view("looks fine")}) != 0
    other = "0" * 40
    assert _run(command, tmp_path, {"pr-view": _view(f"Lab: head={other}")}) != 0


@pytest.mark.unit
def test_contract_findings_check_passes_only_with_a_findings_5_to_10_comment(
    tmp_path: Path,
) -> None:
    command = _command(*_FINDINGS_ITEM)
    holds = _view("Findings 5-10: each fixed or argued")
    assert _run(command, tmp_path, {"pr-view": holds}) == 0
    assert _run(command, tmp_path, {"pr-view": _view()}) != 0
    assert _run(command, tmp_path, {"pr-view": _view("Lab: head=abc")}) != 0


@pytest.mark.unit
@pytest.mark.parametrize(("contract", "item_id"), _CHECKS_ITEMS)
def test_contract_pr_checks_check_fails_on_a_red_required_context_or_other_head(
    tmp_path: Path, contract: str, item_id: str
) -> None:
    command = _command(contract, item_id)
    head = _head()
    green = _checks(head, [])
    only_dhc = _checks(head, [_DHC])
    assert _run(command, tmp_path, {"pr-checks": green}) == 0
    assert _run(command, tmp_path, {"pr-checks": only_dhc}) == 0
    assert _run(command, tmp_path, {"pr-checks": _checks(head, ["CI Summary"])}) != 0
    assert _run(command, tmp_path, {"pr-checks": _checks(head, [_DHC, "X"])}) != 0
    assert _run(command, tmp_path, {"pr-checks": _checks(head, [], ["Tests"])}) != 0
    assert _run(command, tmp_path, {"pr-checks": _checks("0" * 40, [])}) != 0


@pytest.mark.unit
def test_contract_mergeable_check_fails_on_a_blocked_or_other_head(
    tmp_path: Path,
) -> None:
    command = _command("OMN-24794", "dod-omn24794-mergeable-local")
    head = _head()

    def status(sha: str, **extra: object) -> dict[str, object]:
        base = {"state": "OPEN", "head_sha": sha, "draft": False, "blocked": False}
        return {"result": {**base, **extra}}

    assert _run(command, tmp_path, {"pr-status": status(head)}) == 0
    assert _run(command, tmp_path, {"pr-status": status(head, blocked=True)}) != 0
    assert _run(command, tmp_path, {"pr-status": status(head, draft=True)}) != 0
    assert _run(command, tmp_path, {"pr-status": status("0" * 40)}) != 0


@pytest.mark.unit
def test_contract_falsifier_names_the_fixer_lane_of_the_review() -> None:
    text = (_REPO_ROOT / "contracts" / "OMN-24794.yaml").read_text(encoding="utf-8")
    assert "unblk5609-0751" in text
    assert "unblk5609-1125" not in text


@pytest.mark.unit
def test_runtime_handler_path_change_needs_an_integration_proof_item() -> None:
    """The gate behind contracts/OMN-20201.yaml's integration-proof item.

    A branch named for OMN-20201 that changes a runtime path under
    src/omnimarket/handlers is refused unless the contract of that ticket
    carries an integration-proof item scoped local_done_gate.
    """
    text = (_REPO_ROOT / "contracts" / "OMN-20201.yaml").read_text(encoding="utf-8")
    request = ModelIntegrationProofRequest(
        ticket_id="OMN-20201", contract_text=text, changed_paths=(_RUNTIME_HELPER,)
    )
    assert check_integration_proof(request).failure is None
    document = yaml.safe_load(text)
    document["dod_evidence"] = [
        item
        for item in document["dod_evidence"]
        if "integration-proof" not in item["id"]
    ]
    stripped = ModelIntegrationProofRequest(
        ticket_id="OMN-20201",
        contract_text=yaml.safe_dump(document),
        changed_paths=(_RUNTIME_HELPER,),
    )
    failure = check_integration_proof(stripped).failure
    assert failure is not None
    assert "integration proof" in failure
