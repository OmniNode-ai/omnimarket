# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20070: repo-owned acceptance evidence and its base-branch caller."""

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

from omnimarket.nodes.node_dod_verify.services.scoped_item_binding import (
    scoped_item_binding_violation,
)
from omnimarket.occ_contract_pin import criteria_by_label, criterion_hash
from tests.ci.receipt_gate_pin import RECEIPT_GATE_PIN_TAG

pytestmark = pytest.mark.unit

_DEV_MERGE_TAG = r"dev-[0-9]{14}-[0-9a-f]{12}"

REPO_ROOT = Path(__file__).resolve().parents[2]
CALLER_PATH = REPO_ROOT / ".github" / "workflows" / "call-repo-evidence-gate.yml"


def test_caller_workflow_shape() -> None:
    text = CALLER_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    # PyYAML 1.1 resolves the bare `on:` key to the boolean True.
    triggers = data.get("on", data.get(True))
    assert isinstance(triggers, dict), "caller must declare a mapping on: block"
    assert "pull_request_target" in triggers, "caller must run from the base branch"
    assert "pull_request" not in triggers, "caller must not use pull_request"
    assert "workflow_run" not in triggers, "caller must not use workflow_run"
    target = triggers["pull_request_target"]
    assert target["branches"] == ["dev", "main"], "caller must target dev and main"
    assert target["types"] == [
        "opened",
        "synchronize",
        "reopened",
        "edited",
        "ready_for_review",
    ], "caller must cover the declared PR activity types"
    assert data["permissions"] == {"contents": "read", "pull-requests": "read"}, (
        "caller permissions must be exactly contents: read and pull-requests: read"
    )
    assert set(data["jobs"]) == {"repo-evidence"}, (
        "caller must have one repo-evidence job"
    )
    job = data["jobs"]["repo-evidence"]
    assert re.fullmatch(
        r"OmniNode-ai/omnibase_core/\.github/workflows/receipt-gate\.yml@"
        r"dev-\d{14}-[0-9a-f]{12}",
        job["uses"],
    ), "receipt-gate reusable must be pinned by a dev-merge tag"
    assert job["with"]["evidence-source"] == "caller", (
        "caller evidence mode is required"
    )
    assert re.fullmatch(_DEV_MERGE_TAG, job["with"]["verifier-tag"]), (
        "verifier-tag must be an omnimarket dev-merge tag"
    )
    assert "verifier-version" not in job["with"], (
        "verifier-version is a warned fallback; the caller passes verifier-tag only"
    )
    budget = job["with"]["check-timeout-seconds"]
    assert isinstance(budget, str), (
        "caller must quote check-timeout-seconds, which the reusable types as a string"
    )
    assert re.fullmatch(r"[1-9][0-9]{0,2}", budget), (
        "caller must pass check-timeout-seconds as a whole number of seconds"
    )
    assert 1 <= int(budget) <= 900, (
        "check-timeout-seconds must be within the reusable 1 to 900 range "
        "(OMN-21166); empty or absent leaves each check on the 30 s default that "
        "failed five dev PRs with check_budget_exceeded (OMN-21977)"
    )
    for key in ("steps", "secrets", "if", "name", "permissions"):
        assert key not in job, f"caller job must not declare {key}"
    assert "secrets: inherit" not in text, "caller must not inherit secrets"


_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")


def _pytest_argv(check_value: str) -> list[str]:
    """The argv of a `uv run pytest` check, after any leading `env VAR=value` prefix.

    `env PYTHONDONTWRITEBYTECODE=1 uv run pytest ...` runs the same test as
    `uv run pytest ...`; the verifier accepts both (omnimarket#4317), so the
    binding check does too. Anything else returns an empty list.
    """
    tokens = shlex.split(check_value)
    if tokens[:1] == ["env"]:
        tokens = tokens[1:]
        while tokens and _ENV_ASSIGNMENT.fullmatch(tokens[0]):
            tokens = tokens[1:]
    if tokens[:2] != ["uv", "run"]:
        return []
    # `uv run --frozen pytest ...` runs the same test: skip uv's own flags.
    rest = tokens[2:]
    while rest and rest[0].startswith("-"):
        rest = rest[1:]
    return ["uv", "run", *rest] if rest[:1] == ["pytest"] else []


def _command_pytest_argv(check_value: str) -> list[str]:
    """The argv of a `uv run pytest` run inside a wrapper command, or an empty list.

    OMN-22496 bound two criteria to a `check_type: command` that makes a
    temporary HOME, writes a .gitconfig into it and then runs `uv run pytest`
    over one repo test file. That is a real pytest run naming a real file, and
    no `test_passes` value can express it, because the evidence is about what
    the HOME contains and `env VAR=value` cannot create a directory. Only
    `test_passes` checks were parsed, so both items read as unbound and this
    assertion reddened every open pull request of the repository at once.

    The loosening is narrow on purpose: a command with no `uv run pytest` in it
    still binds nothing, and a wrapped run still has to name a path that exists
    in the tree, so the planted deploy probe and a wrapped nonexistent file are
    both still refused (the three tests beside this rule).
    """
    index = check_value.find("uv run pytest")
    if index < 0:
        return []
    # The run sits inside the wrapper's quoted -c argument, so the slice ends
    # on that argument's closing quote; shlex would refuse the unbalanced tail.
    try:
        return _pytest_argv(check_value[index:].rstrip("'\""))
    except ValueError:
        # A tail shlex cannot split (a quote opened before the marker, a stray
        # escape) names no argv we can read, so it binds nothing rather than
        # raising out of the assertion and hiding every other contract.
        return []


# OMN-21980: pytest options that take their value as the next token. Without
# this, an option's value reads as a path target, and a value that happens to
# name a directory (`-k tests`) would be accepted as the binding's selector.
_VALUE_OPTIONS = frozenset(
    {"-k", "-m", "-n", "-p", "-c", "--tb", "--rootdir", "--deselect", "--ignore"}
)


def _pytest_targets(argv: list[str]) -> list[str]:
    """The path targets of a pytest argv, in order, with no options or option values.

    `uv run pytest <dir> -k expr -q` targets `<dir>` only: `expr` is `-k`'s value
    and the flags are not paths.
    """
    rest = argv[2:]
    if rest[:1] == ["pytest"]:
        rest = rest[1:]
    targets: list[str] = []
    skip = False
    for token in rest:
        if skip:
            skip = False
            continue
        if token.startswith("-"):
            if token in _VALUE_OPTIONS:
                skip = True
            continue
        targets.append(token)
    return targets


def _is_repo_test_target(path: str) -> bool:
    """Whether a selector names repository test code.

    A file is named by its `.py` suffix or by a `/tests` package path. A
    directory is a pytest target in its own right, and three bindings in two
    merged contracts used one (OMN-21980), so an existing directory under the
    repository root counts as well. A path that is not in the tree never does;
    the caller still checks existence, so this only decides shape.
    """
    if path.endswith(".py") or path.endswith("/tests"):
        return True
    if Path(path).is_absolute():
        return False
    candidate = REPO_ROOT / path
    return candidate.is_dir() and candidate.resolve().is_relative_to(REPO_ROOT)


def contract_binds_violations(name: str, contract: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    criteria = {
        ac["id"]
        for requirement in contract.get("requirements", [])
        for ac in requirement.get("acceptance", [])
    }
    bound: set[str] = set()
    for item in contract.get("dod_evidence", []):
        label = f"{name}:{item['id']}"
        # The verifier's must-fail control only works on merged PRs; such
        # an item would be reported unavailable on an open PR.
        if item["id"].startswith("dod-occ-diff-derived-behavior-proof"):
            violations.append(
                f"{label}: merged-PR must-fail controls are unavailable on open PRs"
            )
        if "ac_bindings" in item:
            violations.append(f"{label}: use binds_ac, not ac_bindings")
        if "binds_ac" not in item:
            continue
        # OMN-22403: the rule lives in the verifier's service module.
        refusal = scoped_item_binding_violation(item)
        if refusal:
            violations.append(f"{label}: {refusal}")
            continue
        bound.update(item["binds_ac"])
        argvs = [
            (
                _pytest_argv(check.get("check_value", ""))
                if check.get("check_type") == "test_passes"
                else _command_pytest_argv(check.get("check_value", ""))
            )
            for check in item.get("checks", [])
            if check.get("check_type") in {"test_passes", "command"}
        ]
        selectors = [
            next(
                (
                    path
                    for path in (
                        token.split("::", 1)[0] for token in _pytest_targets(argv)
                    )
                    if _is_repo_test_target(path)
                ),
                "",
            )
            for argv in argvs
            if argv
        ]
        # A criterion about a merged PR body is bound to a grep over that
        # body, which exits non-zero when the sentence is absent.
        body_checks = [
            check
            for check in item.get("checks", [])
            if check.get("check_type") == "command"
            and check.get("check_value", "").startswith("gh pr view ")
            and "--json body" in check["check_value"]
            and "| grep -Eq " in check["check_value"]
        ]
        if body_checks:
            continue
        if not any(
            selector
            and not Path(selector).is_absolute()
            and (REPO_ROOT / selector).resolve().is_relative_to(REPO_ROOT)
            and (REPO_ROOT / selector).exists()
            for selector in selectors
        ):
            violations.append(
                f"{label}: binds_ac requires uv run pytest evidence naming an existing repo test file"
            )
    if not criteria <= bound:
        violations.append(
            f"{name}: acceptance criteria missing binds_ac: {sorted(criteria - bound)}"
        )
    return violations


def _violation_item_id(violation: str) -> str:
    """The evidence item id a `<contract>:<item>: ...` violation names, or ''."""
    head = violation.split(": ", 1)[0]
    return head.split(":", 1)[1] if ":" in head else ""


def binds_diagnosis(contract: dict[str, Any], violation: str) -> str:
    """What the binding checker read in the item a violation names.

    OMN-22399: a red `test_every_repo_contract_binds_every_criterion` named the
    item and nothing else, so each reader re-derived whether the test file, the
    selector or the check type was the fault. This prints, per check, its type,
    its command, the path targets read from it and whether each exists in the
    tree, so the reader sees the cause from the failure alone.
    """
    item_id = _violation_item_id(violation)
    item = next(
        (i for i in contract.get("dod_evidence", []) if i.get("id") == item_id), None
    )
    if item is None:
        return f"  (no dod_evidence item {item_id!r} in this contract)"
    lines: list[str] = []
    for check in item.get("checks", []):
        kind = check.get("check_type")
        value = check.get("check_value", "")
        if kind != "test_passes":
            lines.append(
                f"  - check_type={kind!r} is not test_passes, so it binds no test file: {value[:120]}"
            )
            continue
        argv = _pytest_argv(value)
        if not argv:
            lines.append(f"  - not a `uv run pytest` command: {value[:120]}")
            continue
        targets = [t.split("::", 1)[0] for t in _pytest_targets(argv)]
        shown = ", ".join(
            f"{t} ({'exists' if (REPO_ROOT / t).exists() else 'MISSING'})"
            for t in targets
        )
        lines.append(f"  - test_passes targets read: [{shown or 'none'}]")
    return "\n".join(lines) or "  (item has no checks)"


def test_every_repo_contract_binds_every_criterion() -> None:
    assert CALLER_PATH.is_file(), "repo-owned evidence requires the caller workflow"
    contracts = sorted((REPO_ROOT / "contracts").glob("OMN-*.yaml"))
    assert contracts, "expected at least one repo-owned contracts/OMN-*.yaml"
    violations: list[str] = []
    for path in contracts:
        contract = yaml.safe_load(path.read_text(encoding="utf-8"))
        for violation in contract_binds_violations(path.name, contract):
            violations.append(
                f"{violation}\n{binds_diagnosis(contract, violation)}"
                if _violation_item_id(violation)
                else violation
            )
    assert not violations, "\n".join(violations)


def test_contract_binds_refuses_a_bound_command_only_deploy_probe() -> None:
    """A command-only deploy probe is evidence the verifier runs, not a binding."""
    contract = {
        "requirements": [{"acceptance": [{"id": "AC1"}]}],
        "dod_evidence": [
            {
                "id": "dod-bound-test",
                "binds_ac": ["AC1"],
                "checks": [
                    {
                        "check_type": "test_passes",
                        "check_value": "uv run pytest tests/ci/test_repo_evidence_caller_workflow.py -q",
                    }
                ],
            },
            {
                "id": "dod-planted-deploy-probe",
                "binds_ac": ["AC1"],
                "checks": [
                    {
                        "check_type": "command",
                        "check_value": "curl -fsSL https://raw.githubusercontent.com/OmniNode-ai/omnimarket/main/pyproject.toml | grep -F '[project]'",
                    }
                ],
            },
        ],
    }
    violations = contract_binds_violations("OMN-0.yaml", contract)
    assert violations == [
        "OMN-0.yaml:dod-planted-deploy-probe: binds_ac requires uv run pytest evidence naming an existing repo test file"
    ]


def test_contract_binds_accepts_an_unbound_command_only_deploy_probe() -> None:
    """The repository's deploy probes carry no binds_ac (OMN-20070)."""
    contract = {
        "requirements": [{"acceptance": [{"id": "AC1"}]}],
        "dod_evidence": [
            {
                "id": "dod-bound-test",
                "binds_ac": ["AC1"],
                "checks": [
                    {
                        "check_type": "test_passes",
                        "check_value": "uv run pytest tests/ci/test_repo_evidence_caller_workflow.py -q",
                    }
                ],
            },
            {
                "id": "dod-planted-deploy-probe",
                "checks": [
                    {
                        "check_type": "command",
                        "check_value": "curl -fsSL https://raw.githubusercontent.com/OmniNode-ai/omnimarket/main/pyproject.toml | grep -F '[project]'",
                    }
                ],
            },
        ],
    }
    violations = contract_binds_violations("OMN-0.yaml", contract)
    assert violations == []


@pytest.mark.parametrize(
    ("check_value", "bound"),
    [
        (
            "env PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/ci/test_repo_evidence_caller_workflow.py -q",
            True,
        ),
        (
            "env A=1 B=2 uv run pytest tests/ci/test_repo_evidence_caller_workflow.py",
            True,
        ),
        (
            "env PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/ci/test_repo_evidence_caller_workflow.py",
            False,
        ),
        ("env uv run pytest tests/ci/no_such_test_file.py", False),
        (
            "uv run --frozen pytest tests/ci/test_repo_evidence_caller_workflow.py -q -n 0 --no-cov",
            True,
        ),
        (
            "uv run --frozen python -m pytest tests/ci/test_repo_evidence_caller_workflow.py",
            False,
        ),
    ],
)
def test_contract_binds_reads_through_an_env_prefix(
    check_value: str, bound: bool
) -> None:
    """An `env VAR=value` prefix runs the same pytest; only the runner and file decide."""
    contract = {
        "requirements": [{"acceptance": [{"id": "AC1"}]}],
        "dod_evidence": [
            {
                "id": "dod-env-prefixed-test",
                "binds_ac": ["AC1"],
                "checks": [{"check_type": "test_passes", "check_value": check_value}],
            },
        ],
    }
    violations = contract_binds_violations("OMN-0.yaml", contract)
    assert (violations == []) is bound


# OMN-20073: omnibase_core#1884 added the dependency-bot and pin-only writer-app
# exemption to the caller-evidence dod-verify job. A pin that predates it makes
# every bot version bump fail for binding no evidence of its own.
# OMN-20543: omnibase_core#1886, a descendant of #1884, gives that job a
# Postgres service, the PG16 server tools and INTEGRATION_POSTGRES_*, so a bound
# test that needs a database runs at the head and in the merge-base control.
# The pin moved to a descendant (omnibase_core#1912, OMN-17427; then omnibase_core#1914, OMN-20074; then omnibase_core#1932, OMN-20032; then omnibase_core#1986, OMN-21146; then omnibase_core#2079, OMN-19513).
_EXEMPT_REUSABLE_TAG = RECEIPT_GATE_PIN_TAG


def test_caller_pins_a_reusable_with_the_bot_bump_exemption() -> None:
    uses = yaml.safe_load(CALLER_PATH.read_text(encoding="utf-8"))["jobs"][
        "repo-evidence"
    ]["uses"]
    assert uses.endswith(f"@{_EXEMPT_REUSABLE_TAG}"), (
        "pin the omnibase_core reusable at or past omnibase_core#1884"
    )


# OMN-20073 (S6 cut-over, AC5): the S5 pilot met its bar and dev branch
# protection no longer requires any OCC context, so the caller turns the
# reusable's difference step off. With it on, the step waits for an
# occ-preflight / eligibility verdict on the head and fails closed without one,
# so the PR that deletes the OCC callers could never pass. The verifier stays an
# omnimarket dev-merge tag that ships the classifier (omnimarket#3277), so the
# step can be turned back on by this one input if the cut-over is rolled back.


def test_caller_stops_comparing_with_occ_after_the_s6_cutover() -> None:
    job = yaml.safe_load(CALLER_PATH.read_text(encoding="utf-8"))["jobs"][
        "repo-evidence"
    ]
    assert job["with"].get("compare-with-occ") == "false", (
        'after the S6 cut-over the caller passes compare-with-occ: "false" '
        "(a quoted string input): there is no OCC verdict left to compare"
    )


# OMN-20070 (AC3): omnimarket#3511 (e565ba5ba) makes the verifier refuse a
# contract that leaves one acceptance criterion unbound. A verifier that predates
# it admits such a contract, so the required repo-evidence / dod-verify check
# would pass a half-bound contract. OMN-21676: the reusable decides that by
# ancestry, not by version number: it refuses a verifier-tag that does not
# descend from the floor commit, so the caller names a dev-merge tag and no
# verifier-version.


def test_caller_installs_its_verifier_from_a_dev_merge_tag() -> None:
    job = yaml.safe_load(CALLER_PATH.read_text(encoding="utf-8"))["jobs"][
        "repo-evidence"
    ]
    assert re.fullmatch(_DEV_MERGE_TAG, job["with"]["verifier-tag"]), (
        "verifier-tag must be dev- followed by 14 digits, a hyphen and 12 lowercase "
        "hex digits"
    )
    assert "verifier-version" not in job["with"], (
        "verifier-version is a warned migration fallback, not a caller input"
    )
    assert re.fullmatch(_DEV_MERGE_TAG, job["uses"].rsplit("@", 1)[1]), (
        "the reusable ref must be an omnibase_core dev-merge tag"
    )


@pytest.mark.parametrize(
    ("tag", "admitted"),
    [
        ("dev-20261101000200-949993edf3b7", True),
        ("0.4.305", False),
        ("dev-20261101000200-949993edf3b", False),
        ("dev-20261101000200-949993EDF3B7", False),
        ("dev-2026110100020-949993edf3b7", False),
        ("e565ba5ba00c5a9201c9eb66c0259840960f4ea4", False),
    ],
)
def test_dev_merge_tag_shape_has_an_admitting_and_refusing_cases(
    tag: str, admitted: bool
) -> None:
    assert (re.fullmatch(_DEV_MERGE_TAG, tag) is not None) is admitted


# OMN-20543 (AC3): the pinned reusable is the one whose dod-verify job gives a
# database test its database. A pin before omnibase_core#1886 runs that test
# with none, so a contract can only scope it local_done_gate, which the hosted
# control refuses as a control that did not run (omnimarket#3369).
# The pin moved to a descendant (omnibase_core#1912, OMN-17427; then omnibase_core#1914, OMN-20074; then omnibase_core#1932, OMN-20032; then omnibase_core#1986, OMN-21146; then omnibase_core#2079, OMN-19513).
_DATABASE_REUSABLE_TAG = RECEIPT_GATE_PIN_TAG


def test_caller_pins_a_reusable_that_gives_bound_tests_a_database() -> None:
    uses = yaml.safe_load(CALLER_PATH.read_text(encoding="utf-8"))["jobs"][
        "repo-evidence"
    ]["uses"]
    assert uses.endswith(f"@{_DATABASE_REUSABLE_TAG}"), (
        "pin the omnibase_core reusable at or past omnibase_core#1886"
    )


# OMN-21980: a `uv run pytest <directory>` binding is a pytest target in its own
# right. The extractor kept only a token ending `.py` or `/tests`, so three
# bindings in two merged contracts (OMN-20413 dod-omn20413-ac3-verdict-base-head,
# OMN-21930 dod-omn21930-ac4 and dod-omn21930-ac6) read as unbound although every
# directory they name is in the tree, and the repo-wide check above failed on a
# clean dev. An absent path must still be refused, and an option's value must
# never be read as the target.


def _dir_selector_contract(selector: str) -> dict[str, Any]:
    return {
        "requirements": [{"acceptance": [{"id": "AC1"}]}],
        "dod_evidence": [
            {
                "id": "dod-dir-selector",
                "binds_ac": ["AC1"],
                "checks": [
                    {"check_type": "test_passes", "check_value": selector},
                ],
            }
        ],
    }


def test_contract_binds_accepts_a_selector_naming_an_existing_directory() -> None:
    contract = _dir_selector_contract(
        "uv run pytest tests/unit/nodes/node_pr_landing_decision_compute -q -p no:randomly"
    )
    assert contract_binds_violations("OMN-0.yaml", contract) == []


def test_contract_binds_refuses_a_selector_naming_a_path_not_in_the_tree() -> None:
    contract = _dir_selector_contract(
        "uv run pytest tests/unit/nodes/node_that_does_not_exist -q"
    )
    assert contract_binds_violations("OMN-0.yaml", contract) == [
        "OMN-0.yaml:dod-dir-selector: binds_ac requires uv run pytest evidence "
        "naming an existing repo test file"
    ]


def _wrapper_command_contract(command: str) -> dict[str, Any]:
    return {
        "requirements": [{"acceptance": [{"id": "AC1"}]}],
        "dod_evidence": [
            {
                "id": "dod-wrapped-pytest",
                "binds_ac": ["AC1"],
                "checks": [{"check_type": "command", "check_value": command}],
            }
        ],
    }


def test_contract_binds_accepts_a_command_that_wraps_uv_run_pytest() -> None:
    """A `command` check that prepares a HOME and then runs pytest binds its file.

    OMN-22496 bound two criteria to exactly this shape: the evidence is about
    what the HOME's .gitconfig contains, so no `test_passes` value can express
    it. Only `test_passes` checks were parsed, so both items read as unbound
    and this assertion reddened every open pull request of the repository.
    """
    contract = _wrapper_command_contract(
        'bash -o pipefail -c \'h=$(mktemp -d) && printf "[tag]\\n\\tgpgsign = true\\n" '
        '> "$h/.gitconfig" && HOME="$h" uv run pytest '
        "tests/ci/test_repo_evidence_caller_workflow.py -q --no-cov'"
    )
    assert contract_binds_violations("OMN-0.yaml", contract) == []


def test_contract_binds_still_refuses_a_command_with_no_pytest_run() -> None:
    """The loosening is narrow: a wrapper command that runs no pytest binds nothing."""
    contract = _wrapper_command_contract(
        "bash -o pipefail -c 'curl -fsSL https://example.invalid/x | grep -F y'"
    )
    assert contract_binds_violations("OMN-0.yaml", contract) == [
        "OMN-0.yaml:dod-wrapped-pytest: binds_ac requires uv run pytest evidence "
        "naming an existing repo test file"
    ]


def test_contract_binds_refuses_a_wrapped_pytest_naming_a_missing_path() -> None:
    """A wrapper is not a way past the existing-file rule."""
    contract = _wrapper_command_contract(
        "bash -c 'HOME=/tmp/x uv run pytest tests/ci/test_no_such_file_exists.py -q'"
    )
    assert contract_binds_violations("OMN-0.yaml", contract) == [
        "OMN-0.yaml:dod-wrapped-pytest: binds_ac requires uv run pytest evidence "
        "naming an existing repo test file"
    ]


def test_contract_binds_does_not_read_an_option_value_as_the_target() -> None:
    """`-k tests` names an expression, not the `tests` directory."""
    contract = _dir_selector_contract("uv run pytest -k tests -q")
    assert contract_binds_violations("OMN-0.yaml", contract) == [
        "OMN-0.yaml:dod-dir-selector: binds_ac requires uv run pytest evidence "
        "naming an existing repo test file"
    ]


def test_contract_binds_refuses_a_nonexistent_test_path() -> None:
    """A bound pytest check naming a path that does not exist is refused."""
    contract = {
        "requirements": [{"acceptance": [{"id": "AC1"}]}],
        "dod_evidence": [
            {
                "id": "dod-missing-path",
                "binds_ac": ["AC1"],
                "checks": [
                    {
                        "check_type": "test_passes",
                        "check_value": "uv run pytest tests/ci/test_no_such_file_exists.py -q",
                    }
                ],
            },
        ],
    }
    violations = contract_binds_violations("OMN-0.yaml", contract)
    assert violations == [
        "OMN-0.yaml:dod-missing-path: binds_ac requires uv run pytest evidence naming an existing repo test file"
    ]


def _omn21733_floor_contract_item() -> dict[str, Any]:
    return {
        "requirements": [{"acceptance": [{"id": "AC66"}]}],
        "dod_evidence": [
            {
                "id": "dod-omn21733-ac66",
                "binds_ac": ["AC66"],
                "checks": [
                    {
                        "check_type": "test_passes",
                        "check_value": (
                            "uv run pytest tests/nodes/node_factory_floor_publish_effect/"
                            "test_floor_p0_page.py -q --no-cov --tb=line "
                            "-k memory_gauge_matches_label"
                        ),
                        "cwd": "${OMNI_HOME}/omnimarket",
                    }
                ],
            }
        ],
    }


def test_omn22399_a_k_selector_with_a_foreign_cwd_binds_an_existing_test_file() -> None:
    """The recorded OMN-21733 AC66 form binds: the file and the `-k` selector exist.

    Evidence (2026-11-03): 21 PRs failed Tests (Split 2/20) with "OMN-21733.yaml:
    dod-omn21733-ac66: binds_ac requires uv run pytest evidence naming an existing
    repo test file" (#4980 #4977 #4974 #4973 #4972 #4971 #4953 #4951 #4949 #4947
    #4942 #4941 #4931 #4923 #4916 #4900 #4896 #4881 #4874 #4858 #4849). The cause
    was the item's earlier `command` grep check, not the multi-token `-k` form or
    the `${OMNI_HOME}/omnimarket` cwd, so this form must read as bound.
    """
    contract = _omn21733_floor_contract_item()
    item = contract["dod_evidence"][0]
    target = "tests/nodes/node_factory_floor_publish_effect/test_floor_p0_page.py"
    assert (REPO_ROOT / target).is_file()
    assert "def test_memory_gauge_matches_label" in (REPO_ROOT / target).read_text(
        encoding="utf-8"
    )
    assert contract_binds_violations("OMN-21733.yaml", contract) == []
    assert _pytest_targets(_pytest_argv(item["checks"][0]["check_value"])) == [target]


def test_omn22399_a_grep_command_binding_fails_and_the_message_names_the_cause() -> (
    None
):
    """The form that reddened dev (a bound `command` grep) still fails, with its cause named."""
    contract = _omn21733_floor_contract_item()
    contract["dod_evidence"][0]["checks"] = [
        {
            "check_type": "command",
            "check_value": "grep -q 'Memory used' src/omnimarket/nodes/node_factory_floor_publish_effect/page/index.html",
            "cwd": ".",
        }
    ]
    violations = contract_binds_violations("OMN-21733.yaml", contract)
    assert len(violations) == 1
    assert violations[0].startswith("OMN-21733.yaml:dod-omn21733-ac66: ")
    diagnosis = binds_diagnosis(contract, violations[0])
    assert "check_type='command' is not test_passes" in diagnosis
    assert "grep -q 'Memory used'" in diagnosis


def test_omn22399_the_diagnosis_marks_a_missing_target() -> None:
    contract = _omn21733_floor_contract_item()
    contract["dod_evidence"][0]["checks"][0]["check_value"] = (
        "uv run pytest tests/nodes/node_factory_floor_publish_effect/test_gone.py -k x"
    )
    violations = contract_binds_violations("OMN-21733.yaml", contract)
    assert len(violations) == 1
    assert "test_gone.py (MISSING)" in binds_diagnosis(contract, violations[0])


# OMN-22403: the hosted receipt gate's own annotation, read from omnimarket#4918
# (run 41703707219, job 126153377625) and, with other ids, from nine more PRs.
_LOCAL_DONE_GATE_REFUSAL = (
    "scoped local_done_gate, which this hosted check runs neither at the head "
    "nor at the merge base; this job gives every bound test a Postgres "
    "(INTEGRATION_POSTGRES_*, PG16 initdb on PATH), so bind a database test "
    "with the default execution_scope"
)


def _contract_with_scoped_item(*, binds: list[str]) -> dict[str, Any]:
    """The shape of omnimarket#4918's contracts/OMN-22159.yaml public-cutover-pending."""
    scoped: dict[str, Any] = {
        "id": "public-cutover-pending",
        "execution_scope": "local_done_gate",
        "checks": [
            {
                "check_type": "test_passes",
                "check_value": "uv run pytest tests/ci/test_repo_evidence_caller_workflow.py -q",
            }
        ],
    }
    if binds:
        scoped["binds_ac"] = binds
    return {
        "requirements": [{"acceptance": [{"id": "AC1"}, {"id": "AC3"}]}],
        "dod_evidence": [
            {
                "id": "hosted-proof",
                "binds_ac": ["AC1", "AC3"],
                "checks": [
                    {
                        "check_type": "test_passes",
                        "check_value": "uv run pytest tests/ci/test_repo_evidence_caller_workflow.py -q",
                    }
                ],
            },
            scoped,
        ],
    }


def test_contract_binds_refuses_a_bound_local_done_gate_item() -> None:
    """The hosted gate cannot control a scoped item, so binding one is refused here first."""
    violations = contract_binds_violations(
        "OMN-22159.yaml", _contract_with_scoped_item(binds=["AC3"])
    )
    assert violations == [
        "OMN-22159.yaml:public-cutover-pending: a local_done_gate item may not carry "
        "binds_ac; " + _LOCAL_DONE_GATE_REFUSAL.split(";")[0]
    ]


def test_contract_binds_accepts_an_unbound_local_done_gate_item() -> None:
    """An unbound or empty-bound scoped item is the dev convention and stays valid."""
    contract = _contract_with_scoped_item(binds=[])
    assert contract_binds_violations("OMN-22159.yaml", contract) == []
    contract["dod_evidence"][1]["binds_ac"] = []
    assert contract_binds_violations("OMN-22159.yaml", contract) == []


def test_no_repo_contract_binds_a_local_done_gate_item() -> None:
    """OMN-22403: no contract on the tree binds an acceptance criterion to a scoped item."""
    violations: list[str] = []
    for path in sorted((REPO_ROOT / "contracts").glob("OMN-*.yaml")):
        contract = yaml.safe_load(path.read_text(encoding="utf-8"))
        violations.extend(
            violation
            for violation in contract_binds_violations(path.name, contract)
            if "a local_done_gate item may not carry binds_ac" in violation
        )
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize(
    "item_id", ["dod-omn22496-signing-on", "dod-omn22496-signing-off"]
)
def test_omn22496_items_bind_their_criterion_to_a_test_passes_check(
    item_id: str,
) -> None:
    """OMN-22702: each OMN-22496 item binds its criterion to pytest evidence."""
    path = REPO_ROOT / "contracts" / "OMN-22496.yaml"
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    item = next(i for i in contract["dod_evidence"] if i["id"] == item_id)
    assert item["binds_ac"], f"{item_id} binds no criterion"
    kinds = [c.get("check_type") for c in item["checks"]]
    assert "test_passes" in kinds, f"{item_id} has no test_passes check: {kinds}"
    assert contract_binds_violations(path.name, contract) == []


# --- the evidence contracts of the OMN-21217 AC2 binding pull request (OMN-25208) ---
#
# Offline cases: no network, no ticket system, no pull request reader. What cannot
# be read here (a pull request's checks, a ticket's live text) is carried by an
# unbound local_done_gate item and by the receipts of contracts/OMN-25198.yaml;
# these cases pin the shape of those and the logic of the checks, each with a
# control that must fail.

_ROOT = REPO_ROOT
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
