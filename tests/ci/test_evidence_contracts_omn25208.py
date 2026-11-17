# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The evidence contracts of the OMN-21217 AC2 binding pull request (OMN-25208).

Every case here is offline and runs wherever the suite runs: no network, no
ticket system, no pull request reader. What cannot be read there (a pull
request's checks, a ticket's live text) is carried by an unbound
``local_done_gate`` item in the contracts and by the receipts recorded in
``contracts/OMN-25198.yaml``; the cases below pin the shape of those and the
logic of the checks, each with a control that must fail.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env
from pydantic import ValidationError

from omnimarket.occ_contract_pin import criteria_by_label, criterion_hash

_ROOT = Path(__file__).resolve().parents[2]
_CONTRACTS = _ROOT / "contracts"
_NAMES = ("OMN-21217", "OMN-25198", "OMN-25208")
_AC2_TEST_FILE = (
    _ROOT
    / "tests"
    / "nodes"
    / "node_delegation_routing_reducer"
    / "test_hosted_ordered_models_omn21217.py"
)
_AC2_TEST = "test_hosted_retry_walks_only_the_selected_keys_ordered_models"
# The verifier runs a check as bash -o pipefail -c (evidence_collector.py:6413
# at the omnimarket tag the repo-evidence caller installs it from).
_VERIFIER_SHELL = ["bash", "-o", "pipefail", "-c"]


def _contract(name: str) -> dict[str, Any]:
    loaded = yaml.safe_load((_CONTRACTS / f"{name}.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _item(name: str, item_id: str) -> dict[str, Any]:
    found = [i for i in _contract(name)["dod_evidence"] if i["id"] == item_id]
    assert len(found) == 1, f"{name}:{item_id} missing or repeated"
    item: dict[str, Any] = found[0]
    return item


def _check_value(name: str, item_id: str) -> str:
    checks = _item(name, item_id)["checks"]
    assert len(checks) == 1
    value = checks[0]["check_value"]
    assert isinstance(value, str)
    return value


def _verifier(command: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*_VERIFIER_SHELL, command],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=scrub_git_location_env(os.environ),
    )


# --- the ticket contract model (finding 4) -----------------------------------


@pytest.mark.parametrize("name", _NAMES)
def test_every_contract_of_this_pull_request_loads_in_the_ticket_contract_model(
    name: str,
) -> None:
    model = ModelTicketContract.model_validate(_contract(name))
    assert model.ticket_id == name


def test_the_ticket_contract_model_refuses_a_malformed_contract() -> None:
    # The control: the loader above is not one that accepts anything.
    broken = _contract("OMN-25198")
    broken["requirements"] = "not a list of requirements"
    with pytest.raises(ValidationError):
        ModelTicketContract.model_validate(broken)


# --- the criterion hash (finding 4) -------------------------------------------


def _hash_receipt() -> dict[str, Any]:
    receipt = _contract("OMN-25198")["context"]["run_receipts"]["criterion_hash"]
    assert isinstance(receipt, dict)
    return receipt


def _provenance_hash() -> str:
    provenance = _contract("OMN-21217")["context"]["ac_binding_provenance"]
    records = provenance["dod-omn21217-ac2-accepted-20261117t0145z"]
    assert len(records) == 1
    assert records[0]["label"] == "AC2"
    return str(records[0]["criterion_hash"])


def test_the_pinned_ac2_line_hashes_to_the_recorded_criterion_hash() -> None:
    receipt = _hash_receipt()
    line = receipt["ticket_line"].strip()
    # The ticket system stores the bracket escaped; the hash covers that form.
    assert "\\[a, b\\]" in line
    computed = criterion_hash(criteria_by_label("- [ ] " + line)["AC2"])
    assert computed == receipt["value"] == _provenance_hash()


def test_the_unescaped_bracket_form_does_not_hash_to_the_recorded_value() -> None:
    # The control: the escaping is part of what is pinned.
    line = (
        _hash_receipt()["ticket_line"].strip().replace("\\[", "[").replace("\\]", "]")
    )
    unescaped = criterion_hash(criteria_by_label("- [ ] " + line)["AC2"])
    assert unescaped != _hash_receipt()["value"]


def test_the_live_ticket_read_is_recorded_with_the_same_line_and_hash() -> None:
    live = _hash_receipt()["live_read"]
    assert (
        live["source"] == "the ticket system's own text for OMN-21217, read by the lane"
    )
    assert re.fullmatch(r"20\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ", live["read_at"])
    assert live["line_equals_ticket_line"] is True
    assert live["hash"] == _hash_receipt()["value"]


def test_the_pinned_criterion_is_the_ticket_line_not_the_checks_extra_assertions() -> (
    None
):
    # The pinned line names the order of the key's models and no model outside
    # the list or house backend. It names no endpoint, backend identity or
    # serialized decision; those are the check's own strictness and are stated
    # as such in the receipt, so widening the criterion needs a re-accepted line.
    line = _hash_receipt()["ticket_line"]
    assert "endpoint" not in line
    assert "backend identity" not in line
    assert "serialized" not in line
    assert "check's own strictness" in _hash_receipt()["note"]


# --- the bound AC2 test (finding 2) -------------------------------------------


def _ac2_violations(source: str) -> list[str]:
    """What the bound AC2 test must do, read from its source."""
    tree = ast.parse(source)
    problems: list[str] = []
    functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    test = functions.get(_AC2_TEST)
    route = functions.get("_route")
    if test is None or route is None:
        return ["the bound test or its _route helper is missing"]

    def attrs(node: ast.AST) -> set[str]:
        return {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}

    def names(node: ast.AST) -> set[str]:
        return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}

    if not {"model_dump_json", "model_validate_json"} <= attrs(route):
        problems.append("the routing decision is not round-tripped through JSON")
    if "_route" not in names(test):
        problems.append("the test does not route through _route")
    real = (
        "omnimarket.nodes.node_delegation_orchestrator.handlers"
        ".handler_delegation_workflow"
    )
    imported = any(
        isinstance(n, ast.ImportFrom)
        and n.module == "omnimarket.nodes.node_delegation_orchestrator.handlers"
        and any(a.name == "handler_delegation_workflow" for a in n.names)
        for n in ast.walk(tree)
    )
    if not imported:
        problems.append(f"the orchestrator is not imported from {real}")
    constructed = any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "HandlerDelegationWorkflow"
        for n in ast.walk(test)
    )
    if not constructed:
        problems.append(
            "the test does not construct the real HandlerDelegationWorkflow"
        )
    for method in (
        "handle_delegation_request",
        "handle_routing_decision",
        "handle_inference_response",
    ):
        if method not in attrs(test):
            problems.append(f"the test never calls the orchestrator's {method}")
    for field in ("selected_backend_id", "selected_backend_ref"):
        asserted = any(
            isinstance(n, ast.Assert) and field in attrs(n) for n in ast.walk(test)
        )
        if not asserted:
            problems.append(f"the test asserts nothing about {field}")
    parametrized = any(
        isinstance(d, ast.Call)
        and isinstance(d.func, ast.Attribute)
        and d.func.attr == "parametrize"
        and [getattr(a, "value", None) for a in getattr(d.args[1], "elts", [])]
        == [False, True]
        for d in test.decorator_list
    )
    if not parametrized:
        problems.append("the test is not parametrized over [False, True]")
    for banned in ("skip", "skipif", "xfail", "Mock", "MagicMock", "patch"):
        if banned in names(tree) | attrs(tree):
            problems.append(f"the bound test file uses {banned}")
    return problems


def test_the_bound_ac2_test_round_trips_drives_the_real_orchestrator_and_asserts_the_backend() -> (
    None
):
    assert _ac2_violations(_AC2_TEST_FILE.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (
            "HandlerDelegationWorkflow(",
            "StubWorkflow(",
            "real HandlerDelegationWorkflow",
        ),
        ("model_validate_json", "model_validate", "round-tripped through JSON"),
        ("selected_backend_ref", "selected_backend_id", "selected_backend_ref"),
        (
            "handle_inference_response(",
            "handle_inference_stub(",
            "handle_inference_response",
        ),
        ("[False, True]", "[False]", "parametrized over"),
        (
            "import pytest\n",
            "import pytest\nfrom unittest.mock import MagicMock\n\n_m = MagicMock()\n",
            "MagicMock",
        ),
    ],
)
def test_the_ac2_source_check_refuses_a_stubbed_or_weakened_test(
    old: str, new: str, expected: str
) -> None:
    # Each mutant is the bound test with one property removed; the check that
    # accepts the real file must name the removed property.
    source = _AC2_TEST_FILE.read_text(encoding="utf-8")
    assert old in source, old
    mutant = source.replace(old, new)
    problems = _ac2_violations(mutant)
    assert any(expected in problem for problem in problems), problems


_STUB_PLUGINS = {
    "no_retry": (
        "from omnimarket.nodes.node_delegation_orchestrator.handlers import "
        "handler_delegation_workflow as m\n"
        "m.HandlerDelegationWorkflow.handle_inference_response = "
        "lambda self, response: []\n"
    ),
    "no_dispatch": (
        "from omnimarket.nodes.node_delegation_orchestrator.handlers import "
        "handler_delegation_workflow as m\n"
        "m.HandlerDelegationWorkflow.handle_routing_decision = "
        "lambda self, decision: iter(())\n"
    ),
}


@pytest.mark.parametrize("stub", sorted(_STUB_PLUGINS))
def test_the_bound_ac2_test_fails_when_the_orchestrator_is_stubbed(
    tmp_path: Path, stub: str
) -> None:
    (tmp_path / f"stub_{stub}.py").write_text(_STUB_PLUGINS[stub], encoding="utf-8")
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            f"{_AC2_TEST_FILE}::{_AC2_TEST}",
            "-q",
            "-n",
            "0",
            "--no-cov",
            "-p",
            "no:cacheprovider",
            "-p",
            f"stub_{stub}",
        ],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
    )
    assert done.returncode == 1, done.stdout + done.stderr
    assert "2 failed" in done.stdout, done.stdout


# --- the pr-checks check (finding 5) ------------------------------------------

_CHECKS_ITEM = "dod-omn25198-pr-checks"


def _pr_checks_filter() -> str:
    value = _check_value("OMN-25198", _CHECKS_ITEM)
    source, _, rest = value.partition(" | ")
    assert source.startswith("onex scm pr-checks OmniNode-ai/omnimarket#6358")
    assert rest.startswith("python3 -c ")
    return rest


def _checks_json(**changes: Any) -> str:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()
    result: dict[str, Any] = {
        "head_sha": head,
        "class": "green",
        "red": [],
        "pending": [],
        "cancelled": [],
        "runs": [["CI Summary", "completed", "success", "2026-11-17T05:00:00Z"]],
    }
    result.update(changes)
    return json.dumps({"verb": "pr-checks", "result": result})


def _run_filter(payload: str) -> subprocess.CompletedProcess[str]:
    command = f"printf '%s' {shlex.quote(payload)} | {_pr_checks_filter()}"
    return _verifier(command, _ROOT)


def test_the_pr_checks_check_passes_only_a_green_read_at_this_head() -> None:
    done = _run_filter(_checks_json())
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize(
    "changes",
    [
        {"class": "red", "red": ["CI Summary"]},
        {"pending": ["Tests (Split 2/20)"]},
        {"cancelled": ["Tests (Split 2/20)"]},
        {"head_sha": "0" * 40},
        {"runs": []},
    ],
)
def test_the_pr_checks_check_fails_on_a_red_pending_cancelled_or_stale_read(
    changes: dict[str, Any],
) -> None:
    # `onex scm pr-checks` itself exits 0 on a red read (read 2026-11-17 at
    # head 2bf71927f97e: class red, exit 0), so the status is read from its
    # JSON here and the check's own exit status carries the verdict.
    done = _run_filter(_checks_json(**changes))
    assert done.returncode == 1, done.stdout + done.stderr


def test_the_pr_checks_read_carries_no_comment_so_the_check_does_not_claim_one() -> (
    None
):
    # `onex scm pr-view` returns state, head and CI class and no comment text,
    # so no lane reader can see a `Lab: head=` comment; the contract says so.
    description = _item("OMN-25198", "dod-omn25198-lab-comment")["description"]
    assert "cannot read pull request comments" in description
    value = _check_value("OMN-25198", "dod-omn25198-lab-comment")
    assert "Lab: head=" not in value


# --- the commit message (finding 7) -------------------------------------------

_TRAILER_ITEM = "dod-omn25198-commit-trailers"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={
            **scrub_git_location_env(os.environ),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "HOME": str(repo),
        },
    ).stdout.strip()


def _trailer_repo(tmp_path: Path, messages: list[str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "dev")
    (repo / "f").write_text("0", encoding="utf-8")
    _git(repo, "add", "f")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/dev", "HEAD")
    for index, message in enumerate(messages, start=1):
        (repo / "f").write_text(str(index), encoding="utf-8")
        _git(repo, "commit", "-q", "-a", "-m", message)
    return repo


_FIX = "Binds-Merged-Fix: OMN-21217 baa835fa277a9885b0c1caac7398e5ce6ca52a2a"


def _trailer_check(repo: Path) -> subprocess.CompletedProcess[str]:
    return _verifier(_check_value("OMN-25198", _TRAILER_ITEM), repo)


def test_the_trailer_check_passes_one_fix_named_once_in_a_trailer_block(
    tmp_path: Path,
) -> None:
    repo = _trailer_repo(
        tmp_path,
        [f"one\n\nbody\n\nTicket: OMN-25198\n{_FIX}", "two\n\nTicket: OMN-25208"],
    )
    done = _trailer_check(repo)
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize(
    "messages",
    [
        # a second, different merged fix in the range
        [
            f"one\n\nTicket: OMN-25198\n{_FIX}",
            "two\n\nBinds-Merged-Fix: OMN-21217 " + "1" * 40,
        ],
        # no merged fix at all
        ["one\n\nTicket: OMN-25198"],
        # the token outside the trailer block, in prose
        [f"one\n\nthe Binds-Merged-Fix trailer is described here\n\n{_FIX}"],
    ],
)
def test_the_trailer_check_fails_two_fixes_none_or_the_token_in_prose(
    tmp_path: Path, messages: list[str]
) -> None:
    done = _trailer_check(_trailer_repo(tmp_path, messages))
    assert done.returncode != 0, done.stdout + done.stderr


def test_the_gate_reads_trailers_over_the_whole_range_not_the_head_alone() -> None:
    description = _item("OMN-25198", _TRAILER_ITEM)["description"]
    assert "merge base to the head" in description
    assert "receipt-gate.yml:1834" in description


# --- the receipts (finding 6) -------------------------------------------------


def test_the_receipts_are_run_output_on_one_host_with_their_logs_named() -> None:
    receipts = _contract("OMN-25198")["context"]["run_receipts"]
    runs = [
        receipts["control_revert"]["reverted_run"],
        receipts["control_revert"]["unreverted_run"],
    ]
    for run in runs:
        assert run["host"] == "h051"
        assert isinstance(run["exit_code"], int)
        assert re.search(r"\d+ (passed|failed) in \d+\.\d+s", run["summary_line"])
        assert "Lab: head=" in run["log"]
    assert runs[0]["exit_code"] == 1
    assert "2 failed" in runs[0]["summary_line"]
    assert runs[1]["exit_code"] == 0
    assert "2 passed" in runs[1]["summary_line"]
    for ancestry in receipts["ancestry"]:
        assert ancestry["host"] == "h051"
    assert [a["exit_code"] for a in receipts["ancestry"]] == [0, 1]


def test_the_exclusion_rule_is_cited_from_the_gate_source_at_a_pinned_revision() -> (
    None
):
    rule = _contract("OMN-25198")["context"]["run_receipts"]["exclusion_rule"]
    assert rule["revision"] == "dev-20261113222915-b853619df394"
    assert rule["repository"] == "omnibase_core"
    assert rule["file"] == ".github/workflows/receipt-gate.yml"
    assert rule["lines"] == "1892-1977"
    other = _contract("OMN-25198")["context"]["run_receipts"]["exclusion_rule_verifier"]
    assert other["revision"] == "dev-20261101000200-949993edf3b7"
    assert (
        other["file"] == "src/omnimarket/nodes/node_dod_verify/services/occ_replay.py"
    )


def test_the_failure_causes_are_the_gate_texts_read_from_the_runs() -> None:
    failure = _contract("OMN-25198")["context"]["run_receipts"]["original_failure"]
    earlier = failure["earlier_head"]
    assert earlier["gate_text"] == (
        "Binds-Merged-Fix names more than one merged fix; name exactly one"
    )
    assert earlier["log_read"].startswith("HTTP 404")
    later = failure["head_2bf71927f97e"]
    assert later["failed_context"] == "Tests (Split 2/20)"
    assert later["failed_tests"] == [
        "tests/ci/test_repo_evidence_caller_workflow.py::test_every_repo_contract_binds_every_criterion",
        "tests/ci/test_repo_evidence_caller_workflow.py::test_no_repo_contract_binds_a_local_done_gate_item",
    ]
    assert later["dod_verify_result"] == "success"
    assert "may not carry binds_ac" in later["error_text"]


# --- the non-blocking findings (findings 8 to 11) -----------------------------


def test_the_follow_up_for_ac5_is_recorded_with_what_was_searched() -> None:
    follow = _contract("OMN-25198")["context"]["follow_ups"][0]
    assert follow["subject"].startswith("AC5 of OMN-21217")
    assert follow["existing_ticket_or_pull_request"] == "none found"
    assert follow["searched"]
    assert "for the orchestrator to file" in follow["action"]


def test_the_verifier_budget_and_the_nested_run_time_are_recorded() -> None:
    budget = _contract("OMN-25198")["context"]["run_receipts"]["check_budget"]
    assert budget["seconds"] == 300
    assert budget["source"].endswith("check-timeout-seconds")
    assert budget["nested_selection_seconds"] < budget["seconds"]


def test_the_reacceptance_decision_is_recorded_with_its_source() -> None:
    decision = _contract("OMN-25198")["context"]["run_receipts"]["reacceptance"]
    assert decision["needed"] is False
    assert "criterion text" in decision["reason"]
    assert decision["source"].startswith("src/omnimarket/occ_ac_transcription.py")


def test_this_contract_binds_every_criterion_to_a_test_in_this_file_or_its_sibling() -> (
    None
):
    contract = _contract("OMN-25208")
    criteria = {
        ac["id"]
        for requirement in contract["requirements"]
        for ac in requirement["acceptance"]
    }
    assert criteria == {f"AC{n}" for n in range(1, 9)}
    bound = {ac for item in contract["dod_evidence"] for ac in item.get("binds_ac", [])}
    assert bound == criteria
