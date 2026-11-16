# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The outbox reuse contracts say what their evidence shows at the merge base.

AC1 of the bound-tests contract wants a non-zero exit at the merge base, so its falsifier is an exit of zero
there, under the one selection its evidence command runs. AC2 of the reuse contract is a preservation
criterion: its behavioural tests also pass at the merge base, which sent every replay.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.handlers.bus_binding import outbox
from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)

pytestmark = pytest.mark.unit

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
REUSE_TESTS = Path(__file__).with_name("test_outbox_refused_receipt_reuse.py")
SELECTION = re.compile(r'-k "([^"]+)"')


def _load(name: str) -> dict[str, Any]:
    loaded = yaml.safe_load((CONTRACTS / name).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _ac(contract: dict[str, Any], ac_id: str) -> str:
    for requirement in contract["requirements"]:
        for criterion in requirement["acceptance"]:
            if criterion["id"] == ac_id:
                return str(criterion["statement"])
    raise AssertionError(ac_id)


def test_contract_omn_24827_ac1_reads_each_bound_selection_exits_non_zero_at_base() -> (
    None
):
    contract = _load("OMN-24827.yaml")
    statement = _ac(contract, "AC1")
    assert "each bound selection" in statement
    assert "exits non-zero" in statement
    assert "every bound test" not in statement
    assert "-- falsifier:" in statement
    assert statement.rstrip().endswith(
        "exits zero with the merge-base outbox module loaded"
    )


def test_contract_omn_24827_ac1_names_one_selection_everywhere() -> None:
    contract = _load("OMN-24827.yaml")
    selections = {
        m.group(1)
        for text in (
            _ac(contract, "AC1"),
            *(req["command"] for req in contract["evidence_requirements"]),
            *(
                check["check_value"]
                for item in contract["dod_evidence"]
                if "AC1" in item.get("binds_ac", [])
                for check in item["checks"]
            ),
        )
        for m in SELECTION.finditer(text)
    }
    assert selections == {"stored_refusal_lookup or no_new_report"}


def test_contract_omn_24250_ac2_description_says_it_is_a_preservation_criterion() -> (
    None
):
    contract = _load("OMN-24250.yaml")
    (item,) = [i for i in contract["dod_evidence"] if i["id"] == "dod-omn24250-ac2"]
    assert "preservation criterion" in item["description"]


def _dod_check(contract: dict[str, Any], item_id: str) -> str:
    (item,) = [i for i in contract["dod_evidence"] if i["id"] == item_id]
    (check,) = item["checks"]
    return str(check["check_value"])


def test_contract_omn_24875_ac4_selection_covers_every_contract_test() -> None:
    """The AC4 selection names the ticket of each ``test_contract_omn_<n>_`` test here, so none is missed."""
    selection = SELECTION.search(
        _dod_check(_load("OMN-24875.yaml"), "dod-omn24875-ac4")
    )
    assert selection is not None
    tickets = {
        name.split("_")[3]
        for name in globals()
        if name.startswith("test_contract_omn_")
    }
    assert tickets == {"24827", "24250", "24875", "24921"}
    for ticket in tickets:
        assert f"omn_{ticket}" in selection.group(1).lower()


def test_contract_omn_24875_ac4_bound_selection_names_tests_in_the_reuse_module() -> (
    None
):
    """Each token of the OMN-24827 bound selection names a test of the reuse module the contracts bind."""
    selection = SELECTION.search(
        _dod_check(_load("OMN-24827.yaml"), "dod-omn24827-ac1")
    )
    assert selection is not None
    names = [
        node.name
        for node in ast.walk(ast.parse(REUSE_TESTS.read_text(encoding="utf-8")))
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        and node.name.startswith("test_")
    ]
    for token in selection.group(1).split(" or "):
        assert [name for name in names if token in name], token


async def test_contract_omn_24875_ac4_outbox_under_test_reuses_a_clash_refusal(
    tmp_path: Path,
) -> None:
    """The bound behaviour runs on the outbox module that is loaded: a clash refusal is not sent twice.

    The contract tests above read only contracts/ and a test file, so they pass whatever outbox module is
    loaded. This one drives that module: the merge-base outbox sends every replay, so it sends twice and fails.
    """
    request_id = UUID("3f1c2a9e-5b7d-4c1e-8a2f-6d4e9b0c7a22")
    request = ModelWorkLedgerAppendRequest(
        request_id=request_id,
        rows=f"2026-11-02T02:41:00Z | FRICTION | lane=writer-lane | req={request_id} | a row\n",
        requested_by_lane="writer-lane",
        requesting_host="h-test",
        requested_at=datetime(2026, 11, 2, 2, 41, tzinfo=UTC),
    )
    calls: list[int] = []

    async def send(
        queued: ModelWorkLedgerAppendRequest,
    ) -> ModelWorkLedgerAppendReceipt:
        calls.append(1)
        return ModelWorkLedgerAppendReceipt(
            request_id=queued.request_id,
            status=EnumWorkLedgerAppendStatus.REFUSED,
            exit_code=65,
            message=f"request id {queued.request_id} already belongs to different rows or principal: x",
            ledger_lines=[],
            ledger_host="h-test",
            duration_ms=1,
        )

    outbox.queue_request(tmp_path, request)
    await outbox.drain(tmp_path, send)
    outbox.queue_request(tmp_path, request)
    result = await outbox.drain(tmp_path, send)

    assert len(calls) == 1
    assert result.refused == 1


STALE = "0" * 40

# (name, onex output with HEAD as {head}, expected exit)
PR_CHECKS_CASES = [
    (
        "green-current-head",
        '{{"result": {{"head_sha": "{head}", "class": "green", "red": [], "pending": []}}}}',
        0,
    ),
    (
        "green-current-head-then-a-text-line",
        '{{"result": {{"head_sha": "{head}", "class": "green", "red": [], "pending": []}}}}\nbus-binding lane=dev binding=durable',
        0,
    ),
    (
        "green-stale-head",
        '{{"result": {{"head_sha": "'
        + STALE
        + '", "class": "green", "red": [], "pending": []}}}}',
        1,
    ),
    (
        "green-no-head-sha",
        '{{"result": {{"class": "green", "red": [], "pending": []}}}}',
        1,
    ),
    (
        "red",
        '{{"result": {{"head_sha": "{head}", "class": "red", "red": ["CI Summary"], "pending": []}}}}',
        1,
    ),
    (
        "pending",
        '{{"result": {{"head_sha": "{head}", "class": "pending", "red": [], "pending": ["Lint"]}}}}',
        1,
    ),
    (
        "class-not-green-no-red-no-pending",
        '{{"result": {{"head_sha": "{head}", "class": "blocked", "red": [], "pending": []}}}}',
        1,
    ),
    (
        "class-missing",
        '{{"result": {{"head_sha": "{head}", "red": [], "pending": []}}}}',
        1,
    ),
    (
        "green-class-with-a-red-context",
        '{{"result": {{"head_sha": "{head}", "class": "green", "red": ["X"], "pending": []}}}}',
        1,
    ),
    ("empty-output", "", 1),
    ("text-only-no-json", "bus-binding lane=dev binding=durable", 1),
    ("broken-json", '{{"result": ', 1),
]
# The two copies of the check: AC5 of the OMN-24875 contract and AC1 of the OMN-24921 contract.
PR_CHECKS_COPIES = [
    ("OMN-24875.yaml", "dod-omn24875-ac5"),
    ("OMN-24921.yaml", "dod-omn24921-ac1"),
]


def _run_pr_checks_check(
    tmp_path: Path, contract: str, item_id: str, output: str
) -> subprocess.CompletedProcess[str]:
    """Run a contract's pr-checks command with a stand-in ``onex`` in a repository whose HEAD is known."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git_env = {
        **scrub_git_location_env(os.environ),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    for argv in (
        ["git", "init", "-q"],
        ["git", "commit", "-q", "--allow-empty", "-m", "head"],
    ):
        subprocess.run(argv, cwd=repo, env=git_env, check=True, capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        env=scrub_git_location_env(git_env),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    fake = tmp_path / "bin" / "onex"
    fake.parent.mkdir()
    body = output.replace("{{", "{").replace("}}", "}").replace("{head}", head)
    fake.write_text(f"#!/bin/sh\ncat <<'EOF'\n{body}\nEOF\n", encoding="utf-8")
    fake.chmod(0o755)
    return subprocess.run(
        _dod_check(_load(contract), item_id),
        shell=True,
        cwd=repo,
        capture_output=True,
        text=True,
        env={
            **scrub_git_location_env(os.environ),
            "PATH": f"{fake.parent}:{os.environ['PATH']}",
        },
        check=False,
    )


@pytest.mark.parametrize(
    ("case", "output", "expected_exit"),
    PR_CHECKS_CASES,
    ids=[case[0] for case in PR_CHECKS_CASES],
)
def test_contract_omn_24875_ac5_pr_checks_check_exits_zero_only_on_the_current_green_head(
    tmp_path: Path, case: str, output: str, expected_exit: int
) -> None:
    """The check bound to AC5 of OMN-24875 exits 0 only on head_sha == git rev-parse HEAD with class green.

    ``onex scm pr-checks`` exits 0 on a red head, so the bound command reads its JSON. A stand-in ``onex``
    prints each state: a read of an older head, an empty read and a class that is not green all exit 1.
    """
    done = _run_pr_checks_check(tmp_path, *PR_CHECKS_COPIES[0], output)
    assert done.returncode == expected_exit, (case, done.stdout, done.stderr)


@pytest.mark.parametrize(
    ("case", "output", "expected_exit"),
    PR_CHECKS_CASES,
    ids=[case[0] for case in PR_CHECKS_CASES],
)
def test_contract_omn_24921_ac1_pr_checks_check_exits_zero_only_on_the_current_green_head(
    tmp_path: Path, case: str, output: str, expected_exit: int
) -> None:
    """The copy of the check bound to AC1 of OMN-24921 reads the head and the class the same way."""
    done = _run_pr_checks_check(tmp_path, *PR_CHECKS_COPIES[1], output)
    assert done.returncode == expected_exit, (case, done.stdout, done.stderr)


def test_contract_pr_checks_check_copies_are_one_command() -> None:
    """Both copies carry the same command, so a fix to one cannot leave the other reading an older head."""
    commands = {
        _dod_check(_load(contract), item_id) for contract, item_id in PR_CHECKS_COPIES
    }
    assert len(commands) == 1


def test_contract_omn_24875_ac4_falsifier_selects_the_outbox_driving_test() -> None:
    """The AC4 falsifier selects the test that drives the loaded outbox module, not only the text tests."""
    selection = SELECTION.search(_ac(_load("OMN-24875.yaml"), "AC4"))
    assert selection is not None
    assert "omn_24875" in selection.group(1).lower()
    names = [n for n in globals() if n.startswith("test_contract_omn_24875_ac4_outbox")]
    assert names


def test_contract_omn_24250_ac1_states_the_six_hour_window() -> None:
    statement = _ac(_load("OMN-24250.yaml"), "AC1")
    assert "within six hours" in statement
    assert f"{int(outbox.REUSE_WINDOW.total_seconds() // 3600)}" == "6"


def test_contract_omn_24921_ac6_says_a_naive_refused_at_is_read_as_utc() -> None:
    statement = _ac(_load("OMN-24921.yaml"), "AC6")
    assert "naive refused_at read as UTC and reused" in statement
    assert "naive refused_at treated as expired" not in statement
