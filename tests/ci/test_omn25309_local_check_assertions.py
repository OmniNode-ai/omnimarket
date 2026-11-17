# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local Done-gate checks of the unimported-handler contracts can fail.

The hosted runner holds no pull request state, so a criterion about a PR comment,
the check-runs or mergeability cannot be bound to a test over the PR. Each such
reading is an unbound ``local_done_gate`` item whose command asserts the fact. These
tests run each of those commands, as written in the contract, with a stub ``onex``
on the path and require exit 0 on a PR that holds the fact and non-zero on one that
does not.

The stub answers are the recordings in ``tests/ci/fixtures/omn25309``: the
``onex scm pr-view``, ``pr-checks`` and ``pr-status`` answers and the
``pr_state_local.py`` line read for omnimarket#5609 at head 013d5c2922. Nothing is
scrubbed except two things the commit hooks refuse: the PR author handle in the
pr-view recording is replaced by ``pr-author``, and the one run row (with its job id
row) of a check named for the bus overlay, which no branch protection requires, is
absent from the pr-checks recording. Where a case needs a fact the recording lacks,
the helper below edits one named field of the recording and says which.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

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
_FIXTURES = _REPO_ROOT / "tests" / "ci" / "fixtures" / "omn25309"
_RECORDED_HEAD = "013d5c2922836ebd3b2ab42b33cfcb1f3ae9628c"
_DHC = "delegation-health-check / Delegation Health Check"
_RUNTIME_HELPER = "src/omnimarket/handlers/unimported_handler_baseline.py"
_REQUIRED = [
    line
    for line in (_FIXTURES / "required_contexts.txt").read_text().splitlines()
    if line
]


def _head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_REPO_ROOT,
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _item(contract: str, item_id: str) -> dict[str, Any]:
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
    assert len(checks) == 1
    return str(checks[0]["check_value"])


def _recorded(name: str, head: str) -> Any:
    """A recorded answer, moved to ``head`` (the recording names the PR head it read)."""
    text = (_FIXTURES / name).read_text(encoding="utf-8")
    return text.replace(_RECORDED_HEAD, head)


def _run(
    command: str,
    tmp_path: Path,
    answers: dict[str, Any],
    *,
    ledger: str = "",
    state_line: str = "",
) -> subprocess.CompletedProcess[str]:
    """Run a contract command with a stub ``onex`` that prints the given answers.

    ``answers`` maps an onex verb to the text the stub prints for it;
    ``state_line`` is what the stub ``pr_state_local.py`` prints.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "onex"
    stub.write_text(
        '#!/bin/sh\n# argv: scm <verb> <target> [--source bus]\ncat "$STUB_DIR/$2.json"\n',
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    for verb, payload in answers.items():
        text = payload if isinstance(payload, str) else json.dumps(payload)
        (bin_dir / f"{verb}.json").write_text(text, encoding="utf-8")
    plugin = tmp_path / "plugin" / "skills" / "merge-drain" / "scripts"
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "pr_state_local.py").write_text(
        "import os\nprint('source: pr-watcher state 2026-11-17T14:22:24Z age=95s')\n"
        "print(os.environ['STUB_STATE_LINE'])\n",
        encoding="utf-8",
    )
    lane_brief = tmp_path / "plugin" / "skills" / "lane-brief" / "scripts"
    lane_brief.mkdir(parents=True, exist_ok=True)
    (lane_brief / "lane_brief.py").write_text(
        "import os, sys\n"
        "rows = os.environ['STUB_LEDGER_ROWS'].splitlines()\n"
        "print('\\n'.join(row for row in rows if sys.argv[2] in row))\n",
        encoding="utf-8",
    )
    env = {
        **scrub_git_location_env(os.environ),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STUB_DIR": str(bin_dir),
        "STUB_STATE_LINE": state_line,
        "OMNI_PLUGIN_ROOT": str(tmp_path / "plugin"),
        "STUB_LEDGER_ROWS": ledger,
    }
    return subprocess.run(
        ["bash", "-c", command],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _view(head: str, *bodies: str) -> dict[str, Any]:
    """The recorded pr-view answer plus ``comments``.

    The watcher form of pr-view carries no comments (the recording has none), so the
    ``comments`` list follows the typed ModelPrComment the bus answer returns.
    """
    view = json.loads(_recorded("pr-view.json", head))
    view["result"]["comments"] = [
        {
            "id": index,
            "author": "reviewer",
            "created_at": "2026-11-17T14:00:00Z",
            "updated_at": "2026-11-17T14:00:00Z",
            "body": body,
            "body_truncated": False,
        }
        for index, body in enumerate(bodies, start=1)
    ]
    return view


def _lab_line(head: str) -> str:
    return f"Lab: head={head} host=h053 exit=0\nFindings 5-10: answered\n"


def _checks(head: str, *, green: bool = True, drop: str = "", skip: str = "") -> Any:
    """The recorded pr-checks answer, edited in the named fields only.

    ``green``: the two recorded failures other than the shared Delegation Health
    Check (CI Summary and Overlay Main Load) read success and leave ``red``.
    ``drop``: one run row removed (a context that never reported).
    ``skip``: one run row's conclusion set to skipped.
    """
    checks = json.loads(_recorded("pr-checks.json", head))
    result = checks["result"]
    if green:
        for row in result["runs"]:
            if row[0] in {"CI Summary", "Overlay Main Load (OMN-23963)"}:
                row[2] = "success"
        result["red"] = [_DHC]
        result["class"] = "red"
    result["runs"] = [row for row in result["runs"] if row[0] != drop]
    for row in result["runs"]:
        if row[0] == skip:
            row[2] = "skipped"
    return checks


_LAB_ITEMS = [
    ("OMN-24837", "dod-omn24837-lab-comment"),
    ("OMN-24794", "dod-omn24794-lab-comment-local"),
    ("OMN-25309", "dod-omn25309-lab-comment-local"),
]
_FOLLOW_ITEMS = [
    ("OMN-24837", "dod-omn24837-non-blocking-findings", "Findings 5-10"),
    ("OMN-24837", "dod-omn24837-count-comment-local", "Exclusion count: 61"),
    ("OMN-25309", "dod-omn25309-gate-comment-local", "Integration-proof gate:"),
]
_CHECKS_ITEMS = [
    ("OMN-24837", "dod-omn24837-required-checks-local"),
    ("OMN-24794", "dod-omn24794-required-checks-local"),
    ("OMN-25309", "dod-omn25309-required-checks-local"),
]


@pytest.mark.unit
@pytest.mark.parametrize(("contract", "item_id"), _LAB_ITEMS)
def test_contract_lab_comment_check_passes_only_with_a_lab_comment_at_head(
    tmp_path: Path, contract: str, item_id: str
) -> None:
    command = _command(contract, item_id)
    head = _head()
    holds = _view(head, "unrelated", _lab_line(head))
    assert _run(command, tmp_path, {"pr-view": holds}).returncode == 0
    assert _run(command, tmp_path, {"pr-view": _view(head)}).returncode != 0
    assert _run(command, tmp_path, {"pr-view": _view(head, "ok")}).returncode != 0
    other = "0" * 40
    other_head = _view(head, f"Lab: head={other}")
    assert _run(command, tmp_path, {"pr-view": other_head}).returncode != 0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("contract", "item_id"),
    [*_LAB_ITEMS, *[(c, i) for c, i, _ in _FOLLOW_ITEMS]],
)
def test_contract_comment_check_refuses_a_lab_line_quoted_mid_comment(
    tmp_path: Path, contract: str, item_id: str
) -> None:
    """A review comment that quotes the Lab line in full is not the Lab comment."""
    command = _command(contract, item_id)
    head = _head()
    quote = (
        "The e33d666 review quoted this line:\n"
        f"{_lab_line(head)}Exclusion count: 61\nIntegration-proof gate: x\n"
    )
    answers = {"pr-view": _view(head, quote)}
    assert _run(command, tmp_path, answers).returncode != 0
    # Control: the same text as the start of a comment passes.
    starts = {
        "pr-view": _view(
            head,
            _lab_line(head) + "Exclusion count: 61\n" + "Integration-proof gate: x\n",
        )
    }
    assert _run(command, tmp_path, starts).returncode == 0


@pytest.mark.unit
@pytest.mark.parametrize(("contract", "item_id", "text"), _FOLLOW_ITEMS)
def test_contract_follow_up_check_needs_its_line_in_the_lab_comment(
    tmp_path: Path, contract: str, item_id: str, text: str
) -> None:
    command = _command(contract, item_id)
    head = _head()
    holds = _view(head, _lab_line(head) + f"{text} stated\n")
    assert _run(command, tmp_path, {"pr-view": holds}).returncode == 0
    lacking = _view(head, f"Lab: head={head} host=h053\n")
    assert _run(command, tmp_path, {"pr-view": lacking}).returncode != 0
    elsewhere = _view(head, f"{text} stated", f"Lab: head={head} host=h053\n")
    assert _run(command, tmp_path, {"pr-view": elsewhere}).returncode != 0


@pytest.mark.unit
@pytest.mark.parametrize(("contract", "item_id"), _CHECKS_ITEMS)
def test_contract_pr_checks_check_reads_the_required_list_at_head(
    tmp_path: Path, contract: str, item_id: str
) -> None:
    command = _command(contract, item_id)
    head = _head()

    def run(checks: Any) -> int:
        return _run(command, tmp_path, {"pr-checks": checks}).returncode

    assert run(_checks(head)) == 0
    # The recording as read: CI Summary and Overlay Main Load were red.
    assert run(_checks(head, green=False)) != 0
    other_run = _checks(head)
    other_run["result"]["red"] = [_DHC, "Overlay Main Load (OMN-23963)"]
    assert run(other_run) != 0
    pending = _checks(head)
    pending["result"]["pending"] = ["Tests (Split 1/20)"]
    assert run(pending) != 0
    assert run(_checks("0" * 40)) != 0
    # A required context that never reported, or was skipped, is not success.
    assert run(_checks(head, drop="state-coverage-gate")) != 0
    assert run(_checks(head, skip="state-coverage-gate")) != 0
    assert run(_checks(head, drop="repo-evidence / dod-verify")) != 0


@pytest.mark.unit
def test_required_list_names_the_branch_protection_and_ruleset_contexts() -> None:
    """The list is the one read with pr_state_local.py --settings on 2026-11-17."""
    assert len(_REQUIRED) == 30
    assert "CI Summary" in _REQUIRED
    assert "repo-evidence / dod-verify" in _REQUIRED
    recorded = json.loads(_recorded("pr-checks.json", _RECORDED_HEAD))
    reported = {row[0] for row in recorded["result"]["runs"]}
    assert set(_REQUIRED) <= reported


@pytest.mark.unit
def test_contract_mergeable_check_reads_the_merge_state_not_the_blocked_flag(
    tmp_path: Path,
) -> None:
    command = _command("OMN-24794", "dod-omn24794-mergeable-local")
    head = _head()
    recorded = _recorded("pr-state-line.txt", head).strip()
    assert "merge_state=BLOCKED" in recorded or "merge_state" not in recorded
    status = json.loads(_recorded("pr-status.json", head))
    # The recorded pr-status of a PR whose merge_state read BLOCKED: blocked is false.
    assert status["result"]["blocked"] is False

    def run(line: str) -> int:
        return _run(command, tmp_path, {}, state_line=line).returncode

    clean = recorded.replace("merge_state=BLOCKED", "merge_state=CLEAN")
    assert run(clean) == 0
    assert run(recorded.replace("merge_state=BLOCKED", "merge_state=DIRTY")) != 0
    assert run(recorded.replace("merge_state=BLOCKED", "merge_state=UNKNOWN")) != 0
    assert run(clean.replace("draft=False", "draft=True")) != 0
    assert run(clean.replace("state=OPEN", "state=MERGED")) != 0
    assert run(clean.replace(head, "0" * 40)) != 0


@pytest.mark.unit
def test_contract_ledger_check_passes_with_a_shared_cause_row_or_green_checks(
    tmp_path: Path,
) -> None:
    command = _command("OMN-24794", "dod-omn24794-ledger-shared-cause-local")
    head = _head()
    row = "2026-11-17T09:14:29Z | STATUS | lane=unblk5609-0751 | cause=x shared=yes | evidence\n"
    red = {"pr-checks": _checks(head, green=False)}
    green = {"pr-checks": _checks(head)}
    assert _run(command, tmp_path, red, ledger=row).returncode == 0
    assert _run(command, tmp_path, green, ledger="").returncode == 0
    assert _run(command, tmp_path, red, ledger="").returncode != 0
    other_lane = row.replace("unblk5609-0751", "other-lane")
    assert _run(command, tmp_path, red, ledger=other_lane).returncode != 0
    no_flag = row.replace("shared=yes", "shared=no")
    assert _run(command, tmp_path, red, ledger=no_flag).returncode != 0


@pytest.mark.unit
@pytest.mark.parametrize(
    "contract", ["OMN-24837", "OMN-24794", "OMN-25309", "OMN-25383"]
)
def test_contract_hosted_criteria_state_no_pull_request_reading(
    contract: str,
) -> None:
    """The PR-state criteria are split from the hosted ones.

    A bound criterion may say only what a hosted test proves; the PR comment, the
    check-runs and the merge state are read by the unbound local items.
    """
    document = yaml.safe_load(
        (_REPO_ROOT / "contracts" / f"{contract}.yaml").read_text(encoding="utf-8")
    )
    for requirement in document["requirements"]:
        for criterion in requirement["acceptance"]:
            statement = criterion["statement"]
            assert "onex scm pr-" not in statement, criterion["id"]
    for item in document["dod_evidence"]:
        if item.get("binds_ac"):
            assert item.get("execution_scope") != "local_done_gate", item["id"]


@pytest.mark.unit
def test_contract_names_the_local_item_for_every_pr_state_criterion() -> None:
    """The three criteria review finding 4 names each have a local check line."""
    for contract, item_id in [
        ("OMN-24794", "dod-omn24794-ledger-shared-cause-local"),
        ("OMN-24837", "dod-omn24837-count-comment-local"),
        ("OMN-25309", "dod-omn25309-gate-comment-local"),
    ]:
        assert _command(contract, item_id)


@pytest.mark.unit
def test_omn23335_contract_reads_the_lab_comment_and_never_the_pr_body() -> None:
    lines = (_REPO_ROOT / "contracts" / "OMN-23335.yaml").read_text().splitlines()
    criterion = next(line for line in lines if "AC2: the remaining" in line)
    assert "Lab: head=" in criterion
    assert "PR body" not in criterion
    note = next(
        line
        for line in lines
        if "dod-omn23335-baseline-count" not in line and "is never edited" in line
    )
    assert "PR body is never edited" in note


@pytest.mark.unit
def test_contract_falsifier_names_the_fixer_lane_of_the_review() -> None:
    text = (_REPO_ROOT / "contracts" / "OMN-24794.yaml").read_text(encoding="utf-8")
    assert "unblk5609-0751" in text
    assert "unblk5609-1125" not in text


@pytest.mark.unit
def test_omn20201_integration_proof_names_the_pr_not_this_branch() -> None:
    text = (_REPO_ROOT / "contracts" / "OMN-20201.yaml").read_text(encoding="utf-8")
    assert "this branch" not in text
    assert "PR 5609" in text


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
