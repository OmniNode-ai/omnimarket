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
from _pytest.mark.expression import Expression
from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env
from pydantic import ValidationError

from omnimarket.nodes.node_dod_verify.services.scoped_item_binding import (
    scoped_item_binding_violation,
)
from omnimarket.occ_contract_pin import criteria_by_label, criterion_hash
from tests.ci.receipt_gate_pin import RECEIPT_GATE_PIN_TAG
from tests.ci.verifier_shell import (
    verifier_shell,
    verifier_shell_at_tag,
    verifier_shell_from_source,
)

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
_NAMES = (
    "OMN-21217",
    "OMN-25198",
    "OMN-25208",
    "OMN-25286",
    "OMN-25316",
    "OMN-25379",
)
_AC2_TEST_FILE = (
    _ROOT
    / "tests"
    / "nodes"
    / "node_delegation_routing_reducer"
    / "test_hosted_ordered_models_omn21217.py"
)
_AC2_TEST = "test_hosted_retry_walks_only_the_selected_keys_ordered_models"
# The shell the verifier runs a command check with, read from the verifier's own
# source (tests/ci/verifier_shell.py), not a literal of this file.
_VERIFIER_SHELL = verifier_shell()


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


# --- the verifier's shell (finding 3) -----------------------------------------


def test_the_verifier_runs_a_command_check_under_pipefail() -> None:
    # Read from the runner's source: every contract check that ends a pipeline in
    # grep depends on this, and the AC2 check of contracts/OMN-21217.yaml is one.
    shell = verifier_shell()
    assert shell[0] == "bash"
    assert "pipefail" in shell
    assert shell[-1] == "-c"


def test_the_verifier_shell_reader_sees_a_runner_that_drops_pipefail() -> None:
    # The control: the reader is not one that always answers pipefail. The same
    # source with the option removed reads as a shell without it, and then a
    # test that runs the pipefail case under it fails.
    source = (
        REPO_ROOT
        / "src/omnimarket/nodes/node_dod_verify/services/evidence_collector.py"
    ).read_text(encoding="utf-8")
    old = '["bash", "-o", "pipefail", "-c", cmd_str]'
    assert source.count(old) == 1
    mutant = verifier_shell_from_source(source.replace(old, '["bash", "-c", cmd_str]'))
    assert "pipefail" not in mutant


def test_the_verifier_shell_reader_refuses_a_source_without_the_runner() -> None:
    with pytest.raises(AssertionError, match="exactly one bash command-check runner"):
        verifier_shell_from_source("import subprocess\nsubprocess.run(['ls'])\n")


def test_a_failed_pytest_that_prints_the_counted_line_fails_under_the_verifier_shell() -> (
    None
):
    # The hazard pipefail closes, run under the shell read from the verifier. A
    # first stage that prints the line and exits 3 must fail the pipeline; under a
    # shell without pipefail it passes (the control).
    command = "(echo '2 passed'; exit 3) | grep -E '^2 passed'"
    guarded = _verifier(command, _ROOT)
    assert guarded.returncode == 3, guarded.stdout + guarded.stderr
    plain = subprocess.run(
        ["bash", "-c", command], capture_output=True, text=True, check=False
    )
    assert plain.returncode == 0


# --- the selections of the checks (finding 2) --------------------------------
#
# A check that selects a test with -k must select only tests this pull request
# adds: a selected test that already passes at the merge base proves nothing about
# the change. The test names of the two files at the merge base are spelled out
# here (a list in this repository, so it runs everywhere, with no history read).
_MERGE_BASE_TESTS = {
    "tests/ci/test_repo_evidence_caller_workflow.py": frozenset(
        {
            "test_caller_installs_its_verifier_from_a_dev_merge_tag",
            "test_caller_pins_a_reusable_that_gives_bound_tests_a_database",
            "test_caller_pins_a_reusable_with_the_bot_bump_exemption",
            "test_caller_stops_comparing_with_occ_after_the_s6_cutover",
            "test_caller_workflow_shape",
            "test_contract_binds_accepts_a_command_that_wraps_uv_run_pytest",
            "test_contract_binds_accepts_a_selector_naming_an_existing_directory",
            "test_contract_binds_accepts_an_unbound_command_only_deploy_probe",
            "test_contract_binds_accepts_an_unbound_local_done_gate_item",
            "test_contract_binds_does_not_read_an_option_value_as_the_target",
            "test_contract_binds_reads_through_an_env_prefix",
            "test_contract_binds_refuses_a_bound_command_only_deploy_probe",
            "test_contract_binds_refuses_a_bound_local_done_gate_item",
            "test_contract_binds_refuses_a_nonexistent_test_path",
            "test_contract_binds_refuses_a_selector_naming_a_path_not_in_the_tree",
            "test_contract_binds_refuses_a_wrapped_pytest_naming_a_missing_path",
            "test_contract_binds_still_refuses_a_command_with_no_pytest_run",
            "test_dev_merge_tag_shape_has_an_admitting_and_refusing_cases",
            "test_every_repo_contract_binds_every_criterion",
            "test_no_repo_contract_binds_a_local_done_gate_item",
            "test_omn22399_a_grep_command_binding_fails_and_the_message_names_the_cause",
            "test_omn22399_a_k_selector_with_a_foreign_cwd_binds_an_existing_test_file",
            "test_omn22399_the_diagnosis_marks_a_missing_target",
            "test_omn22496_items_bind_their_criterion_to_a_test_passes_check",
        }
    ),
    "tests/test_refuse_skips_omn19987.py": frozenset(
        {
            "test_a_real_failure_stays_a_failure",
            "test_a_run_in_which_a_test_did_not_prove_anything_is_refused",
            "test_a_run_in_which_every_test_passes_still_passes",
            "test_a_run_that_selects_no_test_does_not_pass",
            "test_the_same_run_without_the_option_is_unchanged",
            "test_the_shared_conftest_offers_the_option",
        }
    ),
}
_SELECTION_NAMES = ("OMN-25198", "OMN-25208", "OMN-25286", "OMN-25316", "OMN-25379")


def _selected_tests(check_value: str) -> tuple[str, set[str]]:
    """The test file of a `uv run pytest ... -k expr` check and the tests it selects."""
    tokens = shlex.split(check_value.split(" | ")[0])
    paths = [t for t in tokens if t.endswith(".py") or ".py::" in t]
    assert len(paths) == 1, check_value
    path = paths[0].split("::")[0]
    assert "-k" in tokens, f"no -k selection: {check_value}"
    expression = Expression.compile(tokens[tokens.index("-k") + 1])
    module = Path(path).stem
    tree = ast.parse((_ROOT / path).read_text(encoding="utf-8"))
    chosen = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith("test_")
        and expression.evaluate(
            lambda ident, name=node.name: (
                ident.lower() in name.lower() or ident.lower() in module.lower()
            )
        )
    }
    return path, chosen


def _selection_checks() -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    for name in _SELECTION_NAMES:
        for item in _contract(name)["dod_evidence"]:
            for check in item["checks"]:
                if check["check_type"] == "test_passes":
                    found.append((name, item["id"], check["check_value"]))
    return found


def test_this_pull_requests_contracts_select_only_tests_the_pull_request_adds() -> None:
    checks = _selection_checks()
    assert len(checks) >= 15
    problems: list[str] = []
    for name, item_id, value in checks:
        path, chosen = _selected_tests(value)
        if not chosen:
            problems.append(f"{name}:{item_id} selects no test in {path}")
        old = sorted(chosen & _MERGE_BASE_TESTS.get(path, frozenset()))
        if old:
            problems.append(f"{name}:{item_id} selects tests of the merge base: {old}")
    assert not problems, "\n".join(problems)


def test_the_selection_check_refuses_the_selection_the_review_named() -> None:
    # The control: the -k of the earlier head selects two tests that exist at the
    # merge base and pass there, and this reader names both.
    earlier = (
        "uv run pytest tests/ci/test_repo_evidence_caller_workflow.py -q -n 0 "
        "--no-cov -k 'failure_causes or binds_every_criterion or binds_a_local_done_gate'"
    )
    path, chosen = _selected_tests(earlier)
    old = chosen & _MERGE_BASE_TESTS[path]
    assert old == {
        "test_every_repo_contract_binds_every_criterion",
        "test_no_repo_contract_binds_a_local_done_gate_item",
    }
    # and a selection that matches nothing reads as empty, which the check above refuses
    _, nothing = _selected_tests(earlier.replace(earlier.split("-k ")[1], "'zzz_none'"))
    assert nothing == set()


def test_the_merge_base_names_are_not_among_the_tests_this_pull_request_adds() -> None:
    # The list names tests the files held at the merge base; each still exists
    # unchanged, so the list is not stale in a way that hides an overlap.
    for path, names in _MERGE_BASE_TESTS.items():
        tree = ast.parse((_ROOT / path).read_text(encoding="utf-8"))
        present = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        assert names <= present, sorted(names - present)


_MERGE_BASE = "13a7e30415"


def _test_names(source: str) -> set[str]:
    tree = ast.parse(source)
    return {
        n.name
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")
    }


def _names_at_merge_base(path: str) -> set[str] | None:
    """The test names ``path`` held at the merge base, None where the clone lacks it."""
    shown = subprocess.run(
        ["git", "-C", str(_ROOT), "show", f"{_MERGE_BASE}:{path}"],
        capture_output=True,
        text=True,
        check=False,
        env=scrub_git_location_env(os.environ),
    )
    return _test_names(shown.stdout) if shown.returncode == 0 else None


def _merge_base_name_differences(
    listed: dict[str, frozenset[str]], read: Any
) -> list[str]:
    """Names the list omits and names it invents, per file, against ``read(path)``."""
    problems: list[str] = []
    for path, names in sorted(listed.items()):
        actual = read(path)
        omitted = sorted(actual - names)
        invented = sorted(names - actual)
        if omitted:
            problems.append(f"{path}: held at the merge base, not listed: {omitted}")
        if invented:
            problems.append(f"{path}: listed, not held at the merge base: {invented}")
    return problems


def test_the_merge_base_names_equal_the_test_names_of_the_merge_base_commit() -> None:
    # Where the clone holds the merge base commit (the local Done gate and the lab
    # do, through dod-omn25198-merge-base-names, which fails when it does not) the
    # hand-typed list is diffed against `git show <commit>:<file>`: a name left out
    # of the list would make a selection of that merge-base test look like one
    # this pull request adds.
    if _names_at_merge_base(next(iter(_MERGE_BASE_TESTS))) is None:
        return
    problems = _merge_base_name_differences(
        _MERGE_BASE_TESTS, lambda path: _names_at_merge_base(path) or set()
    )
    assert not problems, "\n".join(problems)


def test_the_merge_base_names_diff_sees_an_omitted_and_an_invented_name() -> None:
    # The controls, with no git: the diff is not one that always finds nothing.
    path = "tests/ci/test_repo_evidence_caller_workflow.py"
    listed = _MERGE_BASE_TESTS

    def held(**changes: set[str]) -> Any:
        base = {key: set(value) for key, value in listed.items()}
        base.update(changes)
        return lambda p: base[p]

    # the list as git has it: nothing to report
    assert _merge_base_name_differences(listed, held()) == []
    # one name removed from the list: git still holds it
    first = sorted(listed[path])[0]
    one_removed = {**listed, path: frozenset(sorted(listed[path])[1:])}
    assert _merge_base_name_differences(one_removed, held()) == [
        f"{path}: held at the merge base, not listed: {[first]}"
    ]
    # one name the merge base never held
    assert _merge_base_name_differences(
        listed, held(**{path: set(listed[path]) - {first}})
    ) == [f"{path}: listed, not held at the merge base: {[first]}"]


def test_a_selection_that_matches_no_test_exits_5_and_the_pinned_runner_fails_it() -> (
    None
):
    # At the merge base none of the tests a check selects exists, so its -k matches
    # nothing: pytest exits 5. The runner at the verifier tag fails any non-zero
    # status, so that is the failed control. Run here under that runner's shell.
    value = _check_value("OMN-25286", "dod-omn25286-selection")
    assert " -k 'this_pull_requests_contracts_select" in value
    nothing = value.split(" -k ")[0] + " -k zzz_no_such_test"
    done = subprocess.run(
        [*verifier_shell_at_tag(), nothing],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=200,
        env=scrub_git_location_env(os.environ),
    )
    assert done.returncode == 5, done.stdout + done.stderr
    assert "no tests ran" in done.stdout or "deselected" in done.stdout


def test_every_selection_of_the_contracts_matches_nothing_at_the_merge_base() -> None:
    # The selection each -k makes over the names the merge-base files held is
    # empty, which is the exit 5 above; one name over the list would not be.
    for name, item_id, value in _selection_checks():
        path, _ = _selected_tests(value)
        expression = Expression.compile(shlex.split(value.split(" | ")[0])[-1])
        module = Path(path).stem
        at_base = {
            test
            for test in _MERGE_BASE_TESTS.get(path, frozenset())
            if expression.evaluate(
                lambda ident, test=test, module=module: (
                    ident.lower() in test.lower() or ident.lower() in module.lower()
                )
            )
        }
        assert not at_base, (
            f"{name}:{item_id} selects {sorted(at_base)} at the merge base"
        )


def test_this_pull_requests_contracts_bind_every_criterion_to_a_test_passes_check() -> (
    None
):
    for name in _SELECTION_NAMES:
        contract = _contract(name)
        assert contract_binds_violations(f"{name}.yaml", contract) == [], name
        bound = {ac for i in contract["dod_evidence"] for ac in i.get("binds_ac", [])}
        criteria = {
            ac["id"] for r in contract["requirements"] for ac in r["acceptance"]
        }
        assert bound == criteria, name


def test_this_pull_requests_contracts_carry_no_bound_local_done_gate_item() -> None:
    for name in _SELECTION_NAMES:
        scoped = [
            item["id"]
            for item in _contract(name)["dod_evidence"]
            if item.get("execution_scope") == "local_done_gate"
        ]
        assert scoped, f"{name} has no local_done_gate item"
        for item in _contract(name)["dod_evidence"]:
            if item["id"] in scoped:
                assert not item.get("binds_ac"), item["id"]


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


# The live comparison is the unbound local_done_gate item
# dod-omn25198-live-ac2-hash: it reads the ticket system's text for OMN-21217 and
# hashes its AC2 line. A test that asserts a string typed in the same diff, or that
# a changed line differs from the typed hash, holds for any hash and cannot fail;
# these run the item's own command against a stand-in for the ticket reader and
# fail when any one of the four things it compares moves.

_LIVE_HASH_ITEM = "dod-omn25198-live-ac2-hash"
_LIVE_SEPARATOR = " | uv run --frozen python -c "


def _live_hash_command(description: str, tmp_path: Path, **changes: str) -> str:
    """The item's command with the ticket reader replaced by ``description``.

    ``changes`` rewrites the two contracts it reads, copied under ``tmp_path``:
    ``pin`` the criterion_hash of the OMN-21217 contract, ``value`` the receipt's
    value and ``ticket_line`` the receipt's pinned line.
    """
    value = _check_value("OMN-25198", _LIVE_HASH_ITEM)
    source, separator, rest = value.partition(_LIVE_SEPARATOR)
    assert separator, value
    assert "read_ticket('OMN-21217'" in source
    assert "LINEAR_API_KEY" in source
    receipt = _hash_receipt()
    copies = {
        "OMN-21217": (_CONTRACTS / "OMN-21217.yaml").read_text(encoding="utf-8"),
        "OMN-25198": (_CONTRACTS / "OMN-25198.yaml").read_text(encoding="utf-8"),
    }
    recorded = receipt["value"]
    if "pin" in changes:
        assert copies["OMN-21217"].count(recorded) == 1
        copies["OMN-21217"] = copies["OMN-21217"].replace(recorded, changes["pin"])
    if "value" in changes:
        assert copies["OMN-25198"].count(f'value: "{recorded}"') == 1
        copies["OMN-25198"] = copies["OMN-25198"].replace(
            f'value: "{recorded}"', f'value: "{changes["value"]}"'
        )
    if "ticket_line" in changes:
        line = receipt["ticket_line"].strip()
        assert copies["OMN-25198"].count(line) == 1
        copies["OMN-25198"] = copies["OMN-25198"].replace(line, changes["ticket_line"])
    for name, text in copies.items():
        (tmp_path / f"{name}.yaml").write_text(text, encoding="utf-8")
        old = f"contracts/{name}.yaml"
        assert rest.count(old) == 1, old
        rest = rest.replace(old, str(tmp_path / f"{name}.yaml"))
    payload = json.dumps({"description": description})
    return f"printf '%s' {shlex.quote(payload)} | uv run --frozen python -c {rest}"


def _live_description(line: str) -> str:
    return "- [ ] AC1: a first criterion\n- [ ] " + line.strip() + "\n"


def _run_live_hash(
    tmp_path: Path, description: str, **changes: str
) -> subprocess.CompletedProcess[str]:
    return _verifier(_live_hash_command(description, tmp_path, **changes), _ROOT)


def test_the_live_ac2_hash_check_passes_when_the_live_line_hashes_to_every_recorded_hash(
    tmp_path: Path,
) -> None:
    # The positive control: the check is not one that always fails. The stand-in
    # live text is the pinned line, so this also proves the contract agrees.
    done = _run_live_hash(tmp_path, _live_description(_hash_receipt()["ticket_line"]))
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("never a model outside the list", "sometimes a model outside the list"),
        ("in the order stored", "in any order"),
        ("the same key", "any key"),
    ],
)
def test_the_live_ac2_hash_check_fails_when_the_live_line_changed(
    tmp_path: Path, old: str, new: str
) -> None:
    line = _hash_receipt()["ticket_line"].strip()
    assert old in line, old
    done = _run_live_hash(tmp_path, _live_description(line.replace(old, new)))
    assert done.returncode == 1, done.stdout + done.stderr


def test_the_live_ac2_hash_check_fails_when_the_criterion_hash_changed_over_an_unchanged_line(
    tmp_path: Path,
) -> None:
    done = _run_live_hash(
        tmp_path,
        _live_description(_hash_receipt()["ticket_line"]),
        pin="0" * 64,
    )
    assert done.returncode == 1, done.stdout + done.stderr


def test_the_live_ac2_hash_check_fails_when_the_recorded_value_changed(
    tmp_path: Path,
) -> None:
    done = _run_live_hash(
        tmp_path,
        _live_description(_hash_receipt()["ticket_line"]),
        value="1" * 64,
    )
    assert done.returncode == 1, done.stdout + done.stderr


def test_the_live_ac2_hash_check_fails_when_the_pinned_line_changed_alone(
    tmp_path: Path,
) -> None:
    line = _hash_receipt()["ticket_line"].strip()
    done = _run_live_hash(
        tmp_path,
        _live_description(line),
        ticket_line=line.replace("never a model", "sometimes a model"),
    )
    assert done.returncode == 1, done.stdout + done.stderr


def test_the_live_ac2_hash_check_fails_when_the_live_text_has_no_ac2_line(
    tmp_path: Path,
) -> None:
    done = _run_live_hash(tmp_path, "- [ ] AC1: a first criterion\n")
    assert done.returncode == 1, done.stdout + done.stderr


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
    # The note beside the line is prose; no assertion reads its words. What the
    # line says is pinned by the hash tests above.


# --- the bound AC2 test (finding 2) -------------------------------------------


# The one monkeypatch use the bound test needs: the environment root and the
# same-model reissue budget of the orchestrator. Any other patch, of a handler
# method, the router or the overlay reader, would let the test pass on a stub.
_ALLOWED_MONKEYPATCH = {
    ("setenv", "OMNI_HOME"),
    ("setattr", "byok_backend_max_retries"),
}


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

    def is_call_to(node: ast.AST, attr: str) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == attr
        )

    # The round trip is what _route returns: a returned validate(dump(decision)).
    returns = [n for n in ast.walk(route) if isinstance(n, ast.Return)]
    round_trip_returned = len(returns) == 1 and any(
        is_call_to(call, "model_validate_json")
        and any(is_call_to(arg, "model_dump_json") for arg in call.args)
        for call in ast.walk(returns[0])
    )
    if not round_trip_returned:
        problems.append(
            "the routing decision is not round-tripped through JSON and returned"
        )

    # Every decision handed to the orchestrator is the routed one: _route(...)
    # itself, or a name assigned from it, never the unserialized original.
    routed: set[str] = set()
    for node in ast.walk(test):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "_route"
        ):
            routed |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    decisions = [n for n in ast.walk(test) if is_call_to(n, "handle_routing_decision")]
    if not decisions:
        problems.append(
            "the test never calls the orchestrator's handle_routing_decision"
        )
    for call in decisions:
        arg = call.args[0] if call.args else None
        fed = (
            isinstance(arg, ast.Call)
            and isinstance(arg.func, ast.Name)
            and arg.func.id == "_route"
        ) or (isinstance(arg, ast.Name) and arg.id in routed)
        if not fed:
            problems.append(
                "handle_routing_decision is not fed the decision _route returned"
            )
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
    for method in ("handle_delegation_request", "handle_inference_response"):
        if method not in attrs(test):
            problems.append(f"the test never calls the orchestrator's {method}")

    def asserted_with(attr: str, name: str, *, every_call: bool) -> bool:
        """An assert that compares ``attr`` with ``name`` for equality.

        A comparison with ``!=`` does not count, and for the per-attempt
        attributes the assert must range over every call (a comprehension), so
        an assert on the first call alone does not count either.
        """

        def holds(assertion: ast.Assert) -> bool:
            equal = any(
                isinstance(c, ast.Compare)
                and all(isinstance(op, ast.Eq) for op in c.ops)
                and attr in attrs(c)
                and name in names(c)
                for c in ast.walk(assertion)
            )
            ranges = any(
                isinstance(c, ast.GeneratorExp | ast.ListComp)
                for c in ast.walk(assertion)
            )
            return equal and (ranges or not every_call)

        return any(isinstance(n, ast.Assert) and holds(n) for n in ast.walk(test))

    # The same key and endpoint on every attempt, and the stored backend.
    for attr, name, every_call in (
        ("api_key_ref", "_KEY", True),
        ("base_url", "_ENDPOINT", True),
        ("selected_backend_ref", "_BACKEND", False),
    ):
        if not asserted_with(attr, name, every_call=every_call):
            problems.append(f"the test asserts no {attr} equal to {name}")
    if not any(
        isinstance(n, ast.Assert)
        and any(
            isinstance(c, ast.Compare)
            and all(isinstance(op, ast.Eq) for op in c.ops)
            and "selected_backend_id" in attrs(c)
            and "initial" in names(c)
            for c in ast.walk(n)
        )
        for n in ast.walk(test)
    ):
        problems.append(
            "the test asserts no selected_backend_id equal to the first decision's"
        )
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
    for banned in (
        "skip",
        "skipif",
        "xfail",
        "Mock",
        "MagicMock",
        "patch",
        "MonkeyPatch",
        "setattr",
        "delattr",
        "__setattr__",
        "__dict__",
    ):
        # ``monkeypatch.setattr`` is an attribute named setattr and is judged
        # below by its target; a bare ``setattr(...)`` or ``object.__setattr__``
        # is a stub that no target check sees.
        direct_monkeypatch = banned in ("setattr", "delattr")
        used = names(tree) | (set() if direct_monkeypatch else attrs(tree))
        if banned in used:
            problems.append(f"the bound test file uses {banned}")
    # A stub put on a class or an instance by assignment, as _STUB_PLUGINS does.
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AugAssign | ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            # ``self.rows = ...`` in a helper's own __init__ is construction.
            on_self = isinstance(target, ast.Attribute) and (
                isinstance(target.value, ast.Name) and target.value.id == "self"
            )
            if isinstance(target, ast.Attribute) and not on_self:
                problems.append(
                    f"the bound test file assigns the attribute {target.attr!r}"
                )
    # monkeypatch is only ever called directly: never aliased, passed or read
    # as a bound method, which would hide a patch from the target check below.
    direct_calls = {
        id(n.func.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "monkeypatch"
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and node.id == "monkeypatch"
            and isinstance(node.ctx, ast.Load)
            and id(node) not in direct_calls
        ):
            problems.append("monkeypatch is aliased, passed or read without a call")
    # monkeypatch is used, but only for the two allowed targets.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "monkeypatch"
        ):
            first = node.args[0] if node.args else None
            second = node.args[1] if len(node.args) > 1 else None
            target = None
            if node.func.attr == "setenv" and isinstance(first, ast.Constant):
                target = first.value
            if node.func.attr == "setattr" and isinstance(second, ast.Constant):
                target = second.value
            if (node.func.attr, target) not in _ALLOWED_MONKEYPATCH:
                problems.append(
                    f"the bound test uses monkeypatch.{node.func.attr} on {target!r}"
                )
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
        # The round trip is run and then discarded: the original is returned.
        (
            "    return type(decision).model_validate_json(decision.model_dump_json())",
            "    type(decision).model_validate_json(decision.model_dump_json())\n    return decision",
            "round-tripped through JSON and returned",
        ),
        # The first decision is the unserialized one: the orchestrator is not fed
        # what _route returned.
        (
            "calls = list(handler.handle_routing_decision(initial))",
            "unrouted = routing.delta(request, tenant_overlay=resolve_tenant_overlay("
            "reader, tenant_id=request.tenant_id, task_type=request.task_type, "
            "selector_key=str(request.correlation_id)), "
            "surface=EnumDelegationSurface.CLOUD)\n"
            "    calls = list(handler.handle_routing_decision(unrouted))",
            "not fed the decision _route returned",
        ),
        # The key is not compared with the stored key between attempts.
        (
            "assert all(call.api_key_ref == _KEY for call in calls)",
            "assert all(call.api_key_ref == calls[0].api_key_ref for call in calls)",
            "api_key_ref equal to _KEY",
        ),
        (
            "assert all(call.base_url == _ENDPOINT for call in calls)",
            "assert calls",
            "base_url equal to _ENDPOINT",
        ),
        ("selected_backend_ref", "selected_backend_id", "selected_backend_ref"),
        (
            "handle_inference_response(",
            "handle_inference_stub(",
            "handle_inference_response",
        ),
        # The key is compared with != : the assert is there and holds for any key.
        (
            "assert all(call.api_key_ref == _KEY for call in calls)",
            "assert all(call.api_key_ref != _KEY for call in calls)",
            "api_key_ref equal to _KEY",
        ),
        # The backend id is compared with != : the assert is there and holds for
        # any backend other than the first decision's.
        (
            "assert live.selected_backend_id == initial.selected_backend_id",
            "assert live.selected_backend_id != initial.selected_backend_id",
            "asserts no selected_backend_id equal to the first decision's",
        ),
        # The key is asserted on the first call only, not on every attempt.
        (
            "assert all(call.api_key_ref == _KEY for call in calls)",
            "assert calls[0].api_key_ref == _KEY",
            "api_key_ref equal to _KEY",
        ),
        (
            "assert all(call.base_url == _ENDPOINT for call in calls)",
            "assert calls[0].base_url == _ENDPOINT",
            "base_url equal to _ENDPOINT",
        ),
        # A stub put on the handler class, as _STUB_PLUGINS does, or on the
        # instance, by assignment.
        (
            "    handler.handle_delegation_request(request)\n",
            "    workflow_module.HandlerDelegationWorkflow.handle_inference_response = "
            "lambda self, response: []\n"
            "    handler.handle_delegation_request(request)\n",
            "assigns the attribute 'handle_inference_response'",
        ),
        (
            "    handler.handle_delegation_request(request)\n",
            "    handler.handle_inference_response = lambda response: []\n"
            "    handler.handle_delegation_request(request)\n",
            "assigns the attribute 'handle_inference_response'",
        ),
        # A monkeypatch alias, a bound method read off it and the builtin setattr
        # are not seen by a check that reads only monkeypatch.<call> targets.
        (
            'monkeypatch.setattr(workflow_module, "byok_backend_max_retries", lambda _: 0)',
            "patch_it = monkeypatch\n"
            '    patch_it.setattr(workflow_module.HandlerDelegationWorkflow, "handle_inference_response", lambda self, response: [])',
            "monkeypatch is aliased, passed or read without a call",
        ),
        (
            'monkeypatch.setattr(workflow_module, "byok_backend_max_retries", lambda _: 0)',
            "bound = monkeypatch.setattr\n"
            '    bound(workflow_module.HandlerDelegationWorkflow, "handle_inference_response", lambda self, response: [])',
            "monkeypatch is aliased, passed or read without a call",
        ),
        (
            'monkeypatch.setattr(workflow_module, "byok_backend_max_retries", lambda _: 0)',
            "setattr(workflow_module.HandlerDelegationWorkflow, "
            '"handle_inference_response", lambda self, response: [])',
            "the bound test file uses setattr",
        ),
        ("[False, True]", "[False]", "parametrized over"),
        (
            "import pytest\n",
            "import pytest\nfrom unittest.mock import MagicMock\n\n_m = MagicMock()\n",
            "MagicMock",
        ),
        # monkeypatch on the orchestrator itself, in place of the budget only.
        (
            'monkeypatch.setattr(workflow_module, "byok_backend_max_retries", lambda _: 0)',
            'monkeypatch.setattr(workflow_module.HandlerDelegationWorkflow, "handle_inference_response", lambda self, response: [])',
            "monkeypatch.setattr on 'handle_inference_response'",
        ),
        (
            'monkeypatch.setenv("OMNI_HOME", str(tmp_path))',
            'monkeypatch.setattr(routing, "delta", lambda *a, **k: None)',
            "monkeypatch.setattr on 'delta'",
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


def _run_with_stubbed_orchestrator(tmp_path: Path, stub: str) -> None:
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


# One test per stub: each starts a nested pytest run (about 9 s), and a check that
# selected both would be the longest of the contracts' checks.
def test_the_bound_ac2_test_fails_when_the_orchestrator_drops_the_retry(
    tmp_path: Path,
) -> None:
    _run_with_stubbed_orchestrator(tmp_path, "no_retry")


def test_the_bound_ac2_test_fails_when_the_orchestrator_drops_the_dispatch(
    tmp_path: Path,
) -> None:
    _run_with_stubbed_orchestrator(tmp_path, "no_dispatch")


# --- the pr-checks and pr-view checks (finding 8) ----------------------------

_CHECKS_ITEM = "dod-omn25198-pr-checks"
_VIEW_ITEM = "dod-omn25198-lab-comment"
_CHECKS_SOURCE = "onex scm pr-checks OmniNode-ai/omnimarket#6358"
_VIEW_SOURCE = "onex scm pr-view OmniNode-ai/omnimarket#6358"
_SETTINGS_SOURCE = (
    'python3 "$OMNI_PLUGIN_ROOT/skills/merge-drain/scripts/pr_state_local.py" '
    "--settings OmniNode-ai/omnimarket:dev"
)
_REQUIRED = ["CI Summary", "Wire Compatibility Gate"]
_RULE_REQUIRED = ["repo-evidence / dod-verify"]


def _head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()


def _checks_json(**changes: Any) -> str:
    result: dict[str, Any] = {
        "head_sha": _head(),
        "class": "green-unarmed",
        "red": [],
        "pending": [],
        "cancelled": [],
        "runs": [
            [name, "completed", "success", "2026-11-17T05:00:00Z"]
            for name in [*_REQUIRED, *_RULE_REQUIRED, "Aislop Sweep"]
        ],
    }
    result.update(changes)
    return json.dumps({"verb": "pr-checks", "result": result})


def _settings_text(required: list[str], rule_required: list[str]) -> str:
    return (
        "source: pr-watcher state 2026-11-17T07:26:13Z age=158s\n"
        "omnimarket:dev protection=protected strict=False "
        f"contexts={','.join(required)} required_reviews=0 enforce_admins=True "
        f"rules=required_status_checks rule_contexts={','.join(rule_required)} "
        "rule_strict=False rule_reviews=0 read_at=2026-11-17T06:42:12Z\n"
    )


# The local Done gate runs after the merge (the closer moves a ticket whose pull
# request merged), when the checked-out HEAD is the landing branch's tip: the head
# and the range of commits the items compare are no longer checked out. An item
# that exits 0 on a merged state could never fail when it runs, so each judges the
# merged head_sha's runs (pr-checks) and the merged range's trailers (the branch's
# own commits stay in the clone after a squash merge) instead.


def _view_payload(state: str = "OPEN", head: str | None = None) -> str:
    result: dict[str, Any] = {"state": state}
    if head is not None:
        result["head_sha"] = head
    return json.dumps({"verb": "pr-view", "result": result})


def _with_view_source(value: str, payload: str) -> str:
    """The check with every `onex scm pr-view` read replaced by ``payload``."""
    assert _VIEW_SOURCE in value, value[:200]
    return value.replace(_VIEW_SOURCE, f"printf '%s' {shlex.quote(payload)}")


def _run_checks_check(
    payload: str, settings: str | None = None
) -> subprocess.CompletedProcess[str]:
    """The pr-checks item, with its readers replaced by the given output."""
    value = _check_value("OMN-25198", _CHECKS_ITEM)
    assert " | python3 -c " in value.split(_CHECKS_SOURCE, 1)[1][:20]
    assert _SETTINGS_SOURCE in value
    text = _settings_text(_REQUIRED, _RULE_REQUIRED) if settings is None else settings
    command = value.replace(_CHECKS_SOURCE, f"printf '%s' {shlex.quote(payload)}", 1)
    command = command.replace(_SETTINGS_SOURCE, f"printf '%s' {shlex.quote(text)}", 1)
    return _verifier(command, _ROOT)


def test_the_pr_checks_check_passes_only_a_green_read_at_this_head() -> None:
    done = _run_checks_check(_checks_json())
    assert done.returncode == 0, done.stdout + done.stderr
    for green in ("green", "green-armed"):
        assert _run_checks_check(_checks_json(**{"class": green})).returncode == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"class": "red", "red": ["CI Summary"]},
        {"class": "pending"},
        {"class": "green-held"},
        {"class": "unknown"},
        {"class": ""},
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
    # JSON here and the check's own exit status carries the verdict. Only the
    # three classes that mean every check-run is green are accepted: any other
    # class, including one the watcher adds later, is refused.
    done = _run_checks_check(_checks_json(**changes))
    assert done.returncode == 1, done.stdout + done.stderr


@pytest.mark.parametrize(
    "runs",
    [
        # a required context that has no run
        [["CI Summary", "completed", "success", "t"]],
        # a required context that ran and did not succeed
        [
            ["CI Summary", "completed", "success", "t"],
            ["Wire Compatibility Gate", "completed", "failure", "t"],
            ["repo-evidence / dod-verify", "completed", "success", "t"],
        ],
        # a required context that is still running
        [
            ["CI Summary", "completed", "success", "t"],
            ["Wire Compatibility Gate", "completed", "success", "t"],
            ["repo-evidence / dod-verify", "in_progress", "", "t"],
        ],
    ],
)
def test_the_pr_checks_check_fails_when_a_required_context_has_no_successful_run(
    runs: list[list[str]],
) -> None:
    done = _run_checks_check(_checks_json(runs=runs))
    assert done.returncode == 1, done.stdout + done.stderr


def test_the_pr_checks_check_fails_when_the_required_contexts_cannot_be_read() -> None:
    assert _run_checks_check(_checks_json(), settings="").returncode == 1
    empty = _settings_text([], [])
    assert _run_checks_check(_checks_json(), settings=empty).returncode == 1


def _run_view_check(
    head: str, *, state: str = "OPEN", checks: str | None = None
) -> subprocess.CompletedProcess[str]:
    value = _check_value("OMN-25198", _VIEW_ITEM)
    command = _with_view_source(value, _view_payload(state, head))
    read = _checks_json(head_sha=head) if checks is None else checks
    command = command.replace(_CHECKS_SOURCE, f"printf '%s' {shlex.quote(read)}")
    return _verifier(command, _ROOT)


def test_the_pr_view_check_passes_only_the_pushed_head() -> None:
    done = _run_view_check(_head())
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize("stale", ["0" * 40, "", _head()[:12]])
def test_the_pr_view_check_fails_on_a_stale_head(stale: str) -> None:
    # The pull request reads a head that is not the one checked out here.
    done = _run_view_check(stale)
    assert done.returncode == 1, done.stdout + done.stderr


def test_a_merged_pull_request_is_judged_on_its_merged_head_sha_not_passed() -> None:
    # After the merge HEAD is the landing branch's tip, so the pushed head is not
    # checked out and is not compared. The merged head_sha's runs are judged: a
    # merged read whose required contexts all succeeded passes whatever is
    # checked out, and one with a red or pending context fails.
    merged_head = "0" * 40
    green = _checks_json(**{"class": "merged", "head_sha": merged_head})
    passed = _run_view_check(merged_head, state="MERGED", checks=green)
    assert passed.returncode == 0, passed.stdout + passed.stderr
    for changes in (
        {"red": ["CI Summary"]},
        {"pending": ["CI Summary"]},
        {"cancelled": ["Tests (Split 2/20)"]},
        {"runs": []},
        {"head_sha": "1" * 40},
    ):
        read = _checks_json(**{"class": "merged", "head_sha": merged_head, **changes})
        failed = _run_view_check(merged_head, state="MERGED", checks=read)
        assert failed.returncode == 1, (changes, failed.stdout + failed.stderr)
    red = _run_checks_check(
        _checks_json(**{"class": "merged", "red": ["CI Summary"]}),
    )
    assert red.returncode == 1, red.stdout + red.stderr
    # the controls: an open pull request at a head other than this one still fails
    assert _run_view_check("0" * 40, state="OPEN").returncode == 1
    assert (
        _run_checks_check(
            _checks_json(**{"class": "red", "red": ["CI Summary"]})
        ).returncode
        == 1
    )


def test_a_pull_request_whose_state_cannot_be_read_is_checked_as_open() -> None:
    # An unreadable state is not a merge: the item fails.
    value = _check_value("OMN-25198", _VIEW_ITEM)
    command = value.replace(_VIEW_SOURCE, "printf '%s' 'not json'")
    command = command.replace(_CHECKS_SOURCE, "printf '%s' 'not json'")
    assert _verifier(command, _ROOT).returncode == 1


def test_the_pr_checks_read_carries_no_comment_so_the_check_does_not_claim_one() -> (
    None
):
    # `onex scm pr-view` returns state, head and CI class and no comment text,
    # so no lane reader can see a `Lab: head=` comment; the contract says so.
    description = _item("OMN-25198", _VIEW_ITEM)["description"]
    assert "cannot read pull request comments" in description
    assert "Lab: head=" not in _check_value("OMN-25198", _VIEW_ITEM)


def test_the_live_pr_checks_items_of_both_contracts_are_the_same_check() -> None:
    assert _check_value("OMN-25208", "dod-omn25208-pr-checks-live") == _check_value(
        "OMN-25198", _CHECKS_ITEM
    )
    assert _check_value("OMN-25286", "dod-omn25286-pr-checks-live") == _check_value(
        "OMN-25198", _CHECKS_ITEM
    )
    assert _check_value("OMN-25316", "dod-omn25316-pr-checks-live") == _check_value(
        "OMN-25198", _CHECKS_ITEM
    )
    assert _check_value("OMN-25379", "dod-omn25379-pr-checks-live") == _check_value(
        "OMN-25198", _CHECKS_ITEM
    )


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
# The commit the live check says must not be reachable from the head. A temporary
# repository cannot hold that object, so the check's copy of the name is replaced
# by an object the repository does hold (a commit on a side branch, so it is not an
# ancestor of the head) in the cases that need git to answer 1 and not 128.
_NOT_ANCESTOR = "7965f64668c6"


def _side_commit(repo: Path) -> str:
    """A commit that exists in the repository and is not an ancestor of its head."""
    _git(repo, "switch", "-q", "-c", "side", "origin/dev")
    (repo / "side").write_text("side", encoding="utf-8")
    _git(repo, "add", "side")
    _git(repo, "commit", "-q", "-m", "side")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "dev")
    return sha


def _trailer_check(
    repo: Path, *, not_ancestor: str | None = None, state: str = "OPEN"
) -> subprocess.CompletedProcess[str]:
    value = _check_value("OMN-25198", _TRAILER_ITEM)
    assert value.count(_NOT_ANCESTOR) == 1
    if not_ancestor is not None:
        value = value.replace(_NOT_ANCESTOR, not_ancestor)
    payload = _view_payload(state, _git(repo, "rev-parse", "HEAD"))
    return _verifier(_with_view_source(value, payload), repo)


def test_the_trailer_check_passes_a_merged_range_that_reads_from_the_head_sha(
    tmp_path: Path,
) -> None:
    # An open pull request whose branch is checked out reads its own range; the
    # merged case, where HEAD is the landing tip, is the tests further down.
    repo = _trailer_repo(tmp_path, [f"one\n\nTicket: OMN-25198\n{_FIX}"])
    other = _side_commit(repo)
    done = _trailer_check(repo, not_ancestor=other, state="OPEN")
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_trailer_check_requires_exit_status_1_exactly_from_the_ancestor_test() -> (
    None
):
    value = _check_value("OMN-25198", _TRAILER_ITEM)
    assert f'git merge-base --is-ancestor {_NOT_ANCESTOR} "$h"; test $? -eq 1' in value
    # not the negation, which turns exit 128 (an unknown object) into a pass
    assert f"! git merge-base --is-ancestor {_NOT_ANCESTOR}" not in value


def test_the_trailer_check_passes_one_fix_named_once_in_a_trailer_block(
    tmp_path: Path,
) -> None:
    # The fix is named in the first commit of the range and the second carries no
    # trailer: the check reads the range from the merge base, not the head alone.
    repo = _trailer_repo(
        tmp_path,
        [f"one\n\nbody\n\nTicket: OMN-25198\n{_FIX}", "two\n\nTicket: OMN-25208"],
    )
    other = _side_commit(repo)
    # The control: the object is real, so git answers 1 (not an ancestor) and the
    # check passes; the unchanged check in this repository would exit 128.
    assert _git(repo, "cat-file", "-t", other) == "commit"
    done = _trailer_check(repo, not_ancestor=other)
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_trailer_check_fails_when_the_ancestor_test_exits_128(
    tmp_path: Path,
) -> None:
    # The same repository and history with the object the check names absent: git
    # exits 128 for an unknown object and the check must fail on it.
    repo = _trailer_repo(
        tmp_path,
        [f"one\n\nbody\n\nTicket: OMN-25198\n{_FIX}", "two\n\nTicket: OMN-25208"],
    )
    probe = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", _NOT_ANCESTOR, "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        env=scrub_git_location_env(os.environ),
    )
    assert probe.returncode == 128, probe.stdout + probe.stderr
    done = _trailer_check(repo)
    assert done.returncode == 1, done.stdout + done.stderr


def test_the_trailer_check_fails_when_the_named_commit_is_an_ancestor_of_the_head(
    tmp_path: Path,
) -> None:
    repo = _trailer_repo(
        tmp_path,
        [f"one\n\nbody\n\nTicket: OMN-25198\n{_FIX}", "two\n\nTicket: OMN-25208"],
    )
    first = _git(repo, "rev-parse", "HEAD~1")
    done = _trailer_check(repo, not_ancestor=first)
    assert done.returncode == 1, done.stdout + done.stderr


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
    repo = _trailer_repo(tmp_path, messages)
    other = _side_commit(repo)
    # With a real, non-ancestor object the ancestor test passes, so the exit
    # status 1 comes from the trailer reading and from nothing else.
    done = _trailer_check(repo, not_ancestor=other)
    assert done.returncode == 1, done.stdout + done.stderr


# --- the receipts (finding 6) -------------------------------------------------


def test_the_receipts_are_run_output_on_one_host_with_their_logs_named() -> None:
    receipts = _contract("OMN-25198")["context"]["run_receipts"]
    # The host is stated once for every receipt; a run or ancestry check that
    # carries a host of its own could differ from it, which is what a repeated
    # run on another host looked like.
    assert re.fullmatch(r"h\d{3}", receipts["host"])
    runs = [
        receipts["control_revert"]["reverted_run"],
        receipts["control_revert"]["unreverted_run"],
    ]
    for run in runs:
        assert "host" not in run
        assert isinstance(run["exit_code"], int)
        assert re.search(r"\d+ (passed|failed) in \d+\.\d+s", run["summary_line"])
        # What the log holds is in the pull request comment, which no test here
        # can read; the contract only has to point at one.
        assert isinstance(run["log"], str)
        assert run["log"]
    assert runs[0]["exit_code"] == 1
    assert "2 failed" in runs[0]["summary_line"]
    assert runs[1]["exit_code"] == 0
    assert "2 passed" in runs[1]["summary_line"]
    for ancestry in receipts["ancestry"]:
        assert "host" not in ancestry
    assert [a["exit_code"] for a in receipts["ancestry"]] == [0, 1]
    assert f"on {receipts['host']} " in receipts["lab_run"]
    assert _receipt_host_violations(receipts) == []


def test_no_receipts_carry_a_hand_typed_dod_verify_result() -> None:
    # The dod-verify verdict of a head is read from onex scm pr-checks at that
    # head and attached in the Lab comment; a string typed into any contract of
    # this pull request is not run output and cannot be checked.
    for name in _NAMES:
        text = (_CONTRACTS / f"{name}.yaml").read_text(encoding="utf-8")
        assert "dod_verify_result" not in text, name


def test_the_exclusion_rule_is_cited_from_the_gate_source_at_the_pinned_revision() -> (
    None
):
    rule = _contract("OMN-25198")["context"]["run_receipts"]["exclusion_rule"]
    # The revision is the reusable's pin, read from the test-side copy of it, so
    # advancing the pin without re-reading the cited lines fails here.
    assert rule["revision"] == RECEIPT_GATE_PIN_TAG
    caller = yaml.safe_load(CALLER_PATH.read_text(encoding="utf-8"))
    job = caller["jobs"]["repo-evidence"]
    assert job["uses"].endswith(f"@{RECEIPT_GATE_PIN_TAG}")
    assert rule["repository"] == "omnibase_core"
    assert rule["file"] == ".github/workflows/receipt-gate.yml"
    assert rule["lines"] == "1892-1977"
    other = _contract("OMN-25198")["context"]["run_receipts"]["exclusion_rule_verifier"]
    assert other["revision"] == job["with"]["verifier-tag"]
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
    assert "may not carry binds_ac" in later["error_text"]


# --- the non-blocking findings (findings 8 to 11) -----------------------------


def test_the_follow_up_for_ac5_is_recorded_with_what_was_searched() -> None:
    follow = _contract("OMN-25198")["context"]["follow_ups"][0]
    assert follow["subject"].startswith("AC5 of OMN-21217")
    assert follow["existing_ticket_or_pull_request"] == "none found"
    assert follow["searched"]
    assert "for the orchestrator to file" in follow["action"]
    # The text to file is written out: a title, a parent and criteria that each
    # carry a falsifier.
    proposed = follow["proposed_ticket"]
    assert proposed["title"]
    assert proposed["parent_candidate"] == "OMN-21217"
    assert len(proposed["criteria"]) >= 1
    for criterion in proposed["criteria"]:
        assert re.match(r"AC\d+: ", criterion)
        assert " -- falsifier: " in criterion


def test_the_verifier_budget_and_the_nested_run_time_are_recorded() -> None:
    budget = _contract("OMN-25198")["context"]["run_receipts"]["check_budget"]
    caller = yaml.safe_load(CALLER_PATH.read_text(encoding="utf-8"))
    hosted = int(caller["jobs"]["repo-evidence"]["with"]["check-timeout-seconds"])
    assert budget["seconds"] == hosted
    assert budget["source"].endswith("check-timeout-seconds")
    assert budget["unsplit_selection_seconds"] < budget["seconds"]


def test_the_verifier_budget_of_the_local_done_gate_is_read_from_the_collector() -> (
    None
):
    # The local Done gate runs the same collector, bounded by one function: the
    # default it records is the one in the source. That every check of the
    # contracts fits it is tested per selection below, not asserted here.
    source = (
        REPO_ROOT
        / "src/omnimarket/nodes/node_dod_verify/services/evidence_collector.py"
    ).read_text(encoding="utf-8")
    budget = _contract("OMN-25198")["context"]["run_receipts"]["check_budget"]
    assert budget["local_done_gate_default_seconds"] == _local_done_gate_default(source)
    assert (
        budget["unsplit_selection_seconds"] > budget["local_done_gate_default_seconds"]
    )
    assert "DOD_VERIFY_CHECK_TIMEOUT_S" in source


_SPLIT_ITEM = "dod-omn25198-ac2-check-cannot-skip"


def test_the_ac2_binding_item_is_split_into_checks_that_cover_each_case_once() -> None:
    # The cases that start nested pytest runs are selected in groups small enough
    # for the local Done gate's per-check budget; no case is dropped or run twice.
    checks = _item("OMN-25198", _SPLIT_ITEM)["checks"]
    assert len(checks) >= 4
    groups = [_selected_tests(check["check_value"]) for check in checks]
    sibling = "tests/test_refuse_skips_omn19987.py"
    assert {path for path, _ in groups} == {sibling}
    tree = ast.parse((_ROOT / sibling).read_text(encoding="utf-8"))
    every_case = {
        n.name
        for n in tree.body
        if isinstance(n, ast.FunctionDef)
        and n.name.startswith("test_")
        and "ac2_binding" in n.name
    }
    chosen = [names for _, names in groups]
    assert set().union(*chosen) == every_case
    assert sum(len(names) for names in chosen) == len(every_case)
    # the nested-run cases (about 8 s each) are at most one per check
    nested = [
        name for name in every_case if "old_form" in name or "refusal_stage" in name
    ]
    nested += [name for name in every_case if "prints_no_count" in name]
    nested += [name for name in every_case if "only_one_parametrization_runs" in name]
    for names in chosen:
        assert len(names & set(nested)) <= 1, sorted(names)


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


# --- the fifth review ---------------------------------------------------------
#
# Finding 2: the receipts name one host. Finding 3: every selection of the
# contracts is tested for its run time. Finding 5: the gate's control is read
# from the gate's own source. Finding 6: the reader stage of the live item runs.
# Finding 7: the merged guard is run on real `onex scm pr-view` output.

_HOST_TOKEN = re.compile(r"\bh\d{3}\b")


def _strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [t for k, v in node.items() for t in (*_strings(k), *_strings(v))]
    if isinstance(node, list):
        return [t for v in node for t in _strings(v)]
    return []


def _receipt_host_violations(receipts: dict[str, Any]) -> list[str]:
    """Every host-shaped token of run_receipts that is not the one host it names."""
    host = receipts.get("host")
    if not isinstance(host, str) or not re.fullmatch(r"h\d{3}", host):
        return [f"run_receipts.host is not one host: {host!r}"]
    found = sorted(
        {m for text in _strings(receipts) for m in _HOST_TOKEN.findall(text)}
    )
    return [
        f"run_receipts names {other}, not {host}" for other in found if other != host
    ]


def test_the_run_receipts_name_one_host_and_no_other() -> None:
    receipts = _contract("OMN-25198")["context"]["run_receipts"]
    assert _receipt_host_violations(receipts) == []
    assert receipts["host"] in _strings(receipts)


@pytest.mark.parametrize("where", ["lab_run", "check_budget", "ancestry", "log"])
def test_run_receipts_refuse_any_other_host_wherever_it_is_named(where: str) -> None:
    import copy

    receipts = copy.deepcopy(_contract("OMN-25198")["context"]["run_receipts"])
    other = "h053" if receipts["host"] != "h053" else "h051"
    if where == "lab_run":
        receipts["lab_run"] += f" and on {other}"
    elif where == "check_budget":
        receipts["check_budget"]["note"] = f"took 62 s wall on {other}"
    elif where == "ancestry":
        receipts["ancestry"][1]["object_read"] = f"read on {other}"
    else:
        receipts["control_revert"]["reverted_run"]["log"] += f" run on {other}"
    problems = _receipt_host_violations(receipts)
    assert problems == [f"run_receipts names {other}, not {receipts['host']}"]


def test_run_receipts_refuse_a_host_that_is_not_one_token() -> None:
    assert _receipt_host_violations({"host": "h05"}) != []
    assert _receipt_host_violations({}) != []


# Finding 3: run time of each selection. The local Done gate bounds every check at
# the collector's default; a check that spawns nested pytest runs is the one that
# exceeds it. The seconds below are the `--durations` of one lab run of both test
# files at head 03ef655b49c2 (one host, -n 0); every other test is counted at
# _DEFAULT_TEST_S, and a selection pays _STARTUP_S for the interpreter and the
# imports of its module. A selection must fit _LOCAL_FRACTION of the default.
_STARTUP_S = 8.0
_DEFAULT_TEST_S = 1.0
_LOCAL_FRACTION = 0.8
_MEASURED_TEST_S = {
    "test_the_ac2_binding_check_passes_when_both_parametrizations_run": 11.0,
    "test_the_ac2_binding_check_old_form_exits_zero_when_only_one_runs": 9.4,
    "test_the_ac2_binding_check_fails_when_only_one_parametrization_runs": 9.4,
    "test_the_ac2_binding_check_whole_check_prints_no_count_when_both_skip": 8.2,
    "test_the_ac2_binding_check_refusal_stage_names_both_skips": 7.7,
    "test_the_ac2_binding_check_old_form_exits_zero_when_both_skip": 7.4,
    "test_the_bound_ac2_test_fails_when_the_orchestrator_drops_the_retry": 9.6,
    "test_the_bound_ac2_test_fails_when_the_orchestrator_drops_the_dispatch": 9.3,
    "test_a_selection_that_matches_no_test_exits_5_and_the_pinned_runner_fails_it": 6.0,
    "test_the_ac2_source_check_refuses_a_stubbed_or_weakened_test": 4.0,
}


def _local_done_gate_default(collector_source: str) -> int:
    found = re.search(r"^_DEFAULT_CHECK_TIMEOUT_S = (\d+)$", collector_source, re.M)
    assert found is not None
    return int(found.group(1))


def _local_budget_s() -> float:
    source = (
        REPO_ROOT
        / "src/omnimarket/nodes/node_dod_verify/services/evidence_collector.py"
    ).read_text(encoding="utf-8")
    return _local_done_gate_default(source) * _LOCAL_FRACTION


def _estimated_seconds(check_value: str) -> float:
    _, chosen = _selected_tests(check_value)
    return _STARTUP_S + sum(_MEASURED_TEST_S.get(n, _DEFAULT_TEST_S) for n in chosen)


def test_every_selection_of_every_contract_fits_the_local_done_gate_budget() -> None:
    over = [
        f"{name}:{item_id}: {_estimated_seconds(value):.1f} s over {_local_budget_s():.0f} s: {value.split('-k ')[-1]}"
        for name, item_id, value in _selection_checks()
        if _estimated_seconds(value) > _local_budget_s()
    ]
    assert not over, "\n".join(over)


def test_the_measured_seconds_name_tests_that_exist() -> None:
    present: set[str] = set()
    for path in ("tests/test_refuse_skips_omn19987.py", __file__):
        tree = ast.parse(
            Path(path if path.startswith("/") else _ROOT / path).read_text("utf-8")
        )
        present |= {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert set(_MEASURED_TEST_S) <= present, sorted(set(_MEASURED_TEST_S) - present)


def test_the_unsplit_ac2_selection_is_over_the_budget_and_no_contract_carries_it() -> (
    None
):
    # The control: the selection the review named, every ac2_binding case in one
    # check, is over the budget by the same model, so the model is not one that
    # fits everything; and no check of any contract selects all of those cases.
    unsplit = (
        "uv run pytest tests/test_refuse_skips_omn19987.py -q -n 0 --no-cov -k 'ac2'"
    )
    assert _estimated_seconds(unsplit) > _local_done_gate_default(
        (
            REPO_ROOT
            / "src/omnimarket/nodes/node_dod_verify/services/evidence_collector.py"
        ).read_text(encoding="utf-8")
    )
    tree = ast.parse((_ROOT / "tests/test_refuse_skips_omn19987.py").read_text("utf-8"))
    every_case = {
        n.name
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and "ac2_binding" in n.name
    }
    assert len(every_case) == 10
    for name, item_id, value in _selection_checks():
        _, chosen = _selected_tests(value)
        assert not every_case <= chosen, (
            f"{name}:{item_id} selects every ac2_binding case"
        )


def test_the_ac2_binding_cases_are_covered_once_by_every_contract_that_runs_them() -> (
    None
):
    tree = ast.parse((_ROOT / "tests/test_refuse_skips_omn19987.py").read_text("utf-8"))
    every_case = {
        n.name
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and "ac2_binding" in n.name
    }
    for name in ("OMN-25198", "OMN-25208"):
        chosen: list[set[str]] = []
        for item in _contract(name)["dod_evidence"]:
            for check in item["checks"]:
                if (
                    "test_refuse_skips_omn19987.py" in check["check_value"]
                    and "-k" in check["check_value"]
                ):
                    _, names = _selected_tests(check["check_value"])
                    chosen.append(names & every_case)
        assert set().union(*chosen) == every_case, name
        assert sum(len(c) for c in chosen) == len(every_case), name


def test_the_recorded_split_time_is_within_the_modelled_budget() -> None:
    budget = _contract("OMN-25198")["context"]["run_receipts"]["check_budget"]
    assert budget["split_checks_max_seconds"] <= _local_budget_s()
    longest = max(_estimated_seconds(v) for _, _, v in _selection_checks())
    assert longest <= _local_budget_s()


# Finding 6: the reader stage of the live item. The item's first stage is the
# repository's own ticket reader; the tests above replaced it. Here it runs as
# written, with the handler module replaced by a stand-in that records what it
# was called with, so what `read_ticket` receives and returns is run.

_READER_STUB = """
import json, os, sys, types

module = types.ModuleType(
    "omnimarket.nodes.node_ticket_text_effect.handlers.handler_ticket_text"
)


def read_ticket(ident, url, key):
    with open(os.environ["STUB_CALL_LOG"], "w", encoding="utf-8") as handle:
        json.dump([ident, url, key], handle)
    return {value}


module.read_ticket = read_ticket
sys.modules[module.__name__] = module
"""


def _reader_stage(tmp_path: Path, returns: str) -> subprocess.CompletedProcess[str]:
    value = _check_value("OMN-25198", _LIVE_HASH_ITEM)
    reader, separator, _ = value.partition(_LIVE_SEPARATOR)
    assert separator
    (tmp_path / "sitecustomize.py").write_text(
        _READER_STUB.replace("{value}", returns), encoding="utf-8"
    )
    env = scrub_git_location_env(os.environ)
    env.update(
        {
            "PYTHONPATH": str(tmp_path),
            "LINEAR_API_KEY": "stand-in-key",
            "STUB_CALL_LOG": str(tmp_path / "call.json"),
        }
    )
    return subprocess.run(
        [*_VERIFIER_SHELL, reader],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=env,
    )


def test_the_reader_stage_passes_the_ticket_the_endpoint_and_the_key_and_prints_json(
    tmp_path: Path,
) -> None:
    done = _reader_stage(tmp_path, '{"description": "- [ ] AC2: a line"}')
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads((tmp_path / "call.json").read_text("utf-8")) == [
        "OMN-21217",
        "https://api.linear.app/graphql",
        "stand-in-key",
    ]
    assert json.loads(done.stdout)["description"] == "- [ ] AC2: a line"
    assert "stand-in-key" not in done.stdout + done.stderr


def test_the_reader_stage_fails_when_read_ticket_returns_a_value_json_cannot_hold(
    tmp_path: Path,
) -> None:
    done = _reader_stage(tmp_path, "object()")
    assert done.returncode != 0, done.stdout + done.stderr
    assert "is not JSON serializable" in done.stderr, done.stderr


def test_the_whole_live_item_fails_when_read_ticket_returns_a_value_json_cannot_hold(
    tmp_path: Path,
) -> None:
    value = _check_value("OMN-25198", _LIVE_HASH_ITEM)
    (tmp_path / "sitecustomize.py").write_text(
        _READER_STUB.replace("{value}", "object()"), encoding="utf-8"
    )
    env = scrub_git_location_env(os.environ)
    env.update(
        {
            "PYTHONPATH": str(tmp_path),
            "LINEAR_API_KEY": "stand-in-key",
            "STUB_CALL_LOG": str(tmp_path / "call.json"),
        }
    )
    done = subprocess.run(
        [*_VERIFIER_SHELL, value],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=env,
    )
    assert done.returncode != 0, done.stdout + done.stderr
    assert "is not JSON serializable" in done.stderr, done.stderr


def test_the_reader_stage_fails_when_the_ticket_has_no_description(
    tmp_path: Path,
) -> None:
    # read_ticket answers None for an unknown ticket; the item must not pass on it.
    value = _check_value("OMN-25198", _LIVE_HASH_ITEM)
    (tmp_path / "sitecustomize.py").write_text(
        _READER_STUB.replace("{value}", "None"), encoding="utf-8"
    )
    env = scrub_git_location_env(os.environ)
    env.update(
        {
            "PYTHONPATH": str(tmp_path),
            "LINEAR_API_KEY": "stand-in-key",
            "STUB_CALL_LOG": str(tmp_path / "call.json"),
        }
    )
    done = subprocess.run(
        [*_VERIFIER_SHELL, value],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=env,
    )
    assert done.returncode != 0, done.stdout + done.stderr
    assert "TypeError" in done.stderr, done.stderr


def test_the_real_read_ticket_accepts_the_arguments_the_reader_stage_passes() -> None:
    import inspect

    from omnimarket.nodes.node_ticket_text_effect.handlers.handler_ticket_text import (
        read_ticket,
    )

    bound = inspect.signature(read_ticket).bind(
        "OMN-21217", "https://api.linear.app/graphql", "k"
    )
    assert list(bound.arguments) == ["ident", "url", "key"]
    assert "dict" in str(inspect.signature(read_ticket).return_annotation)


# Finding 7: the merged guard on real output. The fixtures are the output of
# `onex scm pr-view` and `onex scm pr-checks` as the watcher printed it for a
# merged pull request and for this one (the login is replaced; the pr-checks
# fixture's run list is trimmed to six of its runs, so it is not verbatim).

_FIXTURES = REPO_ROOT / "tests" / "ci" / "fixtures"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _run_view_item_with(
    payload: str, checks: str | None = None
) -> subprocess.CompletedProcess[str]:
    command = _with_view_source(_check_value("OMN-25198", _VIEW_ITEM), payload)
    read = _fixture("onex_scm_pr_checks_merged.json") if checks is None else checks
    command = command.replace(_CHECKS_SOURCE, f"printf '%s' {shlex.quote(read)}")
    return _verifier(command, _ROOT)


def test_the_real_pr_view_output_of_a_merged_pull_request_spells_the_state_merged() -> (
    None
):
    merged = json.loads(_fixture("onex_scm_pr_view_merged.json"))
    assert merged["verb"] == "pr-view"
    assert merged["result"]["state"] == "MERGED"
    assert merged["result"]["class"] == "merged"
    assert merged["result"]["merged_at"]
    opened = json.loads(_fixture("onex_scm_pr_view_open.json"))
    assert opened["result"]["state"] == "OPEN"
    assert opened["result"]["class"] != "merged"
    assert not opened["result"]["merged_at"]


def test_the_real_pr_checks_output_of_a_merged_pull_request_spells_the_class_merged() -> (
    None
):
    merged = json.loads(_fixture("onex_scm_pr_checks_merged.json"))["result"]
    assert merged["class"] == "merged"
    # the read of a merged pull request still lists the pending context of its
    # last open read, so a check that reads pending first cannot pass it
    assert merged["pending"]


def test_the_merged_guard_fails_a_real_merged_pr_view_whose_checks_leave_ci_summary_pending() -> (
    None
):
    done = _run_view_item_with(_fixture("onex_scm_pr_view_merged.json"))
    assert done.returncode == 1, done.stdout + done.stderr


def test_the_merged_guard_passes_a_real_merged_pr_view_once_its_head_runs_are_green() -> (
    None
):
    merged = json.loads(_fixture("onex_scm_pr_checks_merged.json"))
    merged["result"]["pending"] = []
    done = _run_view_item_with(
        _fixture("onex_scm_pr_view_merged.json"), checks=json.dumps(merged)
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_merged_guard_does_not_pass_a_real_open_pr_view_at_another_head() -> None:
    # The control: this pull request's own real read, whose head is not the head
    # checked out here, is refused by the item's own check.
    done = _run_view_item_with(_fixture("onex_scm_pr_view_open.json"))
    assert done.returncode == 1, done.stdout + done.stderr


@pytest.mark.parametrize("spelling", ["merged", "Merged", "MERGE", "CLOSED"])
def test_the_pr_view_merged_guard_reads_only_the_spelling_the_real_output_uses(
    spelling: str,
) -> None:
    payload = json.loads(_fixture("onex_scm_pr_view_merged.json"))
    payload["result"]["state"] = spelling
    done = _run_view_item_with(json.dumps(payload))
    assert done.returncode == 1, done.stdout + done.stderr


def test_the_pr_checks_item_fails_the_real_merged_class_with_ci_summary_pending() -> (
    None
):
    # pr-checks spells the class lower case; the read of the merged pull request
    # lists CI Summary pending and no run of it, so the merged head_sha's runs
    # are not all green and the item fails.
    done = _run_checks_check(
        _fixture("onex_scm_pr_checks_merged.json").replace("\n", " ")
    )
    assert done.returncode == 1, done.stdout + done.stderr


@pytest.mark.parametrize("spelling", ["Merged", "MERGED", "merge"])
def test_the_pr_checks_item_does_not_read_another_spelling_of_merged(
    spelling: str,
) -> None:
    # A green read at another head with a spelling of merged that is not the
    # real one is not accepted as merged: its head is compared with this one.
    green = json.loads(_checks_json(**{"head_sha": "0" * 40}))["result"]
    payload = json.dumps({"verb": "pr-checks", "result": {**green, "class": spelling}})
    done = _run_checks_check(payload)
    assert done.returncode == 1, done.stdout + done.stderr


# Finding 5: the gate's control, read from the gate's own source at the pin. The
# control is a full detached worktree of the merge base (or of the fix's parent)
# with the fix's non-test, non-contract paths reverted, and a bound check passes
# the control only by failing in it. The reader below judges a gate source; the
# mutants prove it refuses a control built from a src-only revert; the one read of
# the real file at the pin runs where the clone holds it, and the local item that
# sets REQUIRE_GATE_SOURCE makes the absence of it a failure.

_GATE_FILE = ".github/workflows/receipt-gate.yml"


def _gate_control_violations(source: str) -> list[str]:
    problems: list[str] = []
    prepare = re.search(r"prepare_control\(\) \{(.*?)\n          \}\n", source, re.S)
    if prepare is None:
        return ["the gate has no prepare_control"]
    body = prepare.group(1)
    if 'worktree add --detach "$control_tree" "$MB"' not in body:
        problems.append("the control tree is not a detached worktree of the merge base")
    if re.search(r"sparse|checkout[^\n]*\$MB[^\n]*--\s+src|\bsrc/", body):
        problems.append("the control is built from part of the tree, not the whole")
    if 'diff --binary --no-renames "$fix^1" "$fix" -- "${product[@]}"' not in body:
        problems.append("the revert is not the fix's product diff")
    if "TEST_SIDE|^contracts/OMN-[0-9]+" not in body:
        problems.append(
            "the revert does not exempt exactly test-side paths and contracts"
        )
    if 'checkout "$HEAD_SHA" -- "$path"' not in body:
        problems.append("the head's test-side files are not overlaid on the control")
    step = re.search(r"- name: Must-fail control at the merge base(.*)", source, re.S)
    if step is None:
        return [*problems, "the gate has no must-fail control step"]
    control = step.group(1)
    for needle, why in (
        (
            'export OMNI_HOME="$control_home"',
            "the control does not run in the control tree",
        ),
        (
            "--execution-audience hosted",
            "the control is not run by the hosted verifier",
        ),
        (
            'select(.status == "verified")',
            "a verified bound check is not read as always-pass",
        ),
        (
            'select(.status != "failed" and .status != "verified")',
            "a check that did not fail or verify is not read as not run",
        ),
        (
            "the control did not fail (always-pass)",
            "an always-pass control is not refused",
        ),
        (
            "a control that did not run is not a pass",
            "a control that did not run passes",
        ),
        (
            "TEST_ONLY_DIFF requires verified head evidence",
            "the test-only exception needs no head verdict",
        ),
    ):
        if needle not in control:
            problems.append(why)
    return problems


_GATE_GOOD = """
          prepare_control() {
            git -C "$head_tree" worktree add --detach "$control_tree" "$MB"
            if [[ "$path" =~ $TEST_SIDE|^contracts/OMN-[0-9]+\\.yaml$ ]]; then continue; fi
            git -C "$head_tree" diff --binary --no-renames "$fix^1" "$fix" -- "${product[@]}" | git apply -R
            git -C "$control_tree" checkout "$HEAD_SHA" -- "$path"
          }
      - name: Must-fail control at the merge base
            export OMNI_HOME="$control_home"
            --execution-audience hosted
            select(.status == "verified")
            select(.status != "failed" and .status != "verified")
            the control did not fail (always-pass)
            a control that did not run is not a pass
            TEST_ONLY_DIFF requires verified head evidence
"""


def test_the_gate_control_reader_accepts_a_control_of_the_full_base_tree() -> None:
    assert _gate_control_violations(_GATE_GOOD) == []


@pytest.mark.parametrize(
    ("old", "new", "named"),
    [
        (
            'worktree add --detach "$control_tree" "$MB"',
            'worktree add --detach "$control_tree" "$MB" && git -C "$control_tree" sparse-checkout set src',
            "part of the tree",
        ),
        (
            '-- "${product[@]}"',
            "-- src/",
            "product diff",
        ),
        (
            "TEST_SIDE|^contracts/OMN-[0-9]+",
            "^docs/",
            "exempt exactly test-side",
        ),
        (
            'select(.status != "failed" and .status != "verified")',
            'select(.status == "x")',
            "not run",
        ),
        (
            "the control did not fail (always-pass)",
            "the control passed",
            "always-pass",
        ),
        (
            "--execution-audience hosted",
            "--execution-audience local",
            "hosted verifier",
        ),
    ],
)
def test_the_gate_control_reader_refuses_a_control_built_from_a_src_only_revert(
    old: str, new: str, named: str
) -> None:
    assert old in _GATE_GOOD, old
    problems = _gate_control_violations(_GATE_GOOD.replace(old, new))
    assert any(named in p for p in problems), problems


def _gate_source_at_the_pin() -> str | None:
    clone = Path(os.environ.get("OMNI_HOME", "")) / "omnibase_core"
    shown = subprocess.run(
        ["git", "-C", str(clone), "show", f"{RECEIPT_GATE_PIN_TAG}:{_GATE_FILE}"],
        capture_output=True,
        text=True,
        check=False,
        env=scrub_git_location_env(os.environ),
    )
    return shown.stdout if shown.returncode == 0 else None


def _gate_source_or_none_where_not_required(
    required: bool, source: str | None
) -> str | None:
    if source is None and required:
        raise AssertionError("the gate source at the pin is not in this clone")
    return source


def test_the_gate_source_is_required_where_the_local_item_sets_the_variable() -> None:
    with pytest.raises(AssertionError, match="not in this clone"):
        _gate_source_or_none_where_not_required(True, None)
    assert _gate_source_or_none_where_not_required(False, None) is None


def test_the_gate_control_source_at_the_pin_builds_the_full_base_tree_control() -> None:
    source = _gate_source_or_none_where_not_required(
        os.environ.get("REQUIRE_GATE_SOURCE") == "1", _gate_source_at_the_pin()
    )
    if source is None:
        return
    assert _gate_control_violations(source) == []
    # Every line the receipts cite is that line of the file at the pin.
    receipts = _contract("OMN-25198")["context"]["run_receipts"]
    control = receipts["gate_control"]
    assert control["revision"] == RECEIPT_GATE_PIN_TAG
    assert control["file"] == _GATE_FILE
    lines = source.splitlines()
    for cited in control["cited_lines"]:
        assert cited["text"] in lines[cited["line"] - 1], cited


def test_the_gate_counts_a_nonzero_runner_status_as_a_failed_check_so_exit_5_fails() -> (
    None
):
    from tests.ci.verifier_shell import VERIFIER_TAG_STATUS

    assert VERIFIER_TAG_STATUS[0].strip() == "if result.returncode != 0:"
    done = subprocess.run(
        [*verifier_shell_at_tag(), "exit 5"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 5


# Finding 1: the run the fourth review's premise was about. The record is read
# with `onex scm run-view` (the check-run read of a head other than the pull
# request's current one is refused to a lane); the test pins its shape and that
# the ids it names are the ones its own read command names.
def test_the_split_2_of_20_run_at_63b11bfe0bb4_is_recorded_with_its_ids_and_conclusion() -> (
    None
):
    record = _contract("OMN-25198")["context"]["run_receipts"]["split_2_of_20"]
    assert record["head"] == "63b11bfe0bb4"
    assert record["workflow"] == "CI"
    assert record["check"] == "Tests (Split 2/20)"
    assert isinstance(record["run_id"], int)
    assert isinstance(record["job_id"], int)
    assert record["conclusion"] in {"success", "failure"}
    assert f"--run-id {record['run_id']}" in record["read_with"]
    assert record["read_with"].startswith("onex scm run-view OmniNode-ai/omnimarket ")


def test_the_gate_control_receipt_cites_lines_with_numbers_and_text() -> None:
    # Hosted shape check: the read of the file itself is the local item.
    control = _contract("OMN-25198")["context"]["run_receipts"]["gate_control"]
    assert control["revision"] == RECEIPT_GATE_PIN_TAG
    assert control["file"] == _GATE_FILE
    assert len(control["cited_lines"]) >= 8
    for cited in control["cited_lines"]:
        assert isinstance(cited["line"], int)
        assert cited["text"]
    assert [c["line"] for c in control["cited_lines"]] == sorted(
        c["line"] for c in control["cited_lines"]
    )


# --- review round 6 (findings 2, 3 and 4 of the sixth review) ------------------
# Finding 4: the Done gate runs after the merge, so an item that exits 0 on the
# state MERGED can never fail when it runs. Each item below judges the merged
# head_sha's runs and the merged range's trailers instead.

_PINNED_ITEM_IDS = {
    "OMN-25198": [
        "dod-omn25198-commit-trailers",
        "dod-omn25198-pr-checks",
        "dod-omn25198-lab-comment",
    ],
    "OMN-25208": ["dod-omn25208-pr-checks-live"],
    "OMN-25286": ["dod-omn25286-pr-checks-live"],
    "OMN-25316": ["dod-omn25316-pr-checks-live"],
    "OMN-25379": ["dod-omn25379-pr-checks-live"],
}
_STATE_SHORT_CIRCUIT = "['result']['state']=='MERGED' else 1)"


def test_no_check_of_this_pull_request_exits_0_on_the_state_merged() -> None:
    for name, ids in _PINNED_ITEM_IDS.items():
        for item_id in ids:
            value = _check_value(name, item_id)
            assert _STATE_SHORT_CIRCUIT not in value, f"{name}:{item_id}"
            if "onex scm pr-checks" in value:
                # a merged read is judged on its runs, never passed unread
                assert re.search(r"not [rc]\['pending'\]", value), f"{name}:{item_id}"
                assert re.search(r"not [rc]\['red'\]", value), f"{name}:{item_id}"


def _run_with_sources(
    value: str,
    *,
    view: str,
    checks: str,
    settings: str | None = None,
    cwd: Path = _ROOT,
) -> subprocess.CompletedProcess[str]:
    text = _settings_text(_REQUIRED, _RULE_REQUIRED) if settings is None else settings
    command = value.replace(_SETTINGS_SOURCE, f"printf '%s' {shlex.quote(text)}")
    command = command.replace(_CHECKS_SOURCE, f"printf '%s' {shlex.quote(checks)}")
    command = command.replace(_VIEW_SOURCE, f"printf '%s' {shlex.quote(view)}")
    return _verifier(command, cwd)


def _one_line(payload: str) -> str:
    return json.dumps(json.loads(payload))


@pytest.mark.parametrize(
    ("name", "item_id"),
    [
        ("OMN-25198", "dod-omn25198-pr-checks"),
        ("OMN-25208", "dod-omn25208-pr-checks-live"),
        ("OMN-25286", "dod-omn25286-pr-checks-live"),
        ("OMN-25316", "dod-omn25316-pr-checks-live"),
        ("OMN-25379", "dod-omn25379-pr-checks-live"),
    ],
)
def test_the_live_items_fail_the_real_merged_read_with_ci_summary_pending(
    name: str, item_id: str
) -> None:
    done = _run_with_sources(
        _check_value(name, item_id),
        view=_one_line(_fixture("onex_scm_pr_view_merged.json")),
        checks=_one_line(_fixture("onex_scm_pr_checks_merged.json")),
    )
    assert done.returncode == 1, done.stdout + done.stderr


def test_the_live_item_passes_a_merged_read_whose_head_runs_are_all_green() -> None:
    # the control: the same merged class with every required context successful
    # on the merged head_sha and nothing pending passes, whatever is checked out
    green = json.loads(_fixture("onex_scm_pr_checks_merged.json"))
    green["result"]["pending"] = []
    green["result"]["runs"] = [
        [name, "completed", "success", "t"] for name in [*_REQUIRED, *_RULE_REQUIRED]
    ]
    done = _run_with_sources(
        _check_value("OMN-25198", _CHECKS_ITEM),
        view=_one_line(_fixture("onex_scm_pr_view_merged.json")),
        checks=json.dumps(green),
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_lab_comment_item_fails_a_merged_pull_request_with_ci_summary_pending() -> (
    None
):
    done = _run_with_sources(
        _check_value("OMN-25198", _VIEW_ITEM),
        view=_one_line(_fixture("onex_scm_pr_view_merged.json")),
        checks=_one_line(_fixture("onex_scm_pr_checks_merged.json")),
    )
    assert done.returncode == 1, done.stdout + done.stderr


def _merged_repo(tmp_path: Path, messages: list[str]) -> tuple[Path, str]:
    """A repository whose branch was squash-merged: HEAD is the landing tip and the
    branch's own commits are no longer reachable from it. Returns it and the
    branch's head."""
    repo = _trailer_repo(tmp_path, messages)
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "-c", "landed", "origin/dev")
    (repo / "landed").write_text("squash", encoding="utf-8")
    _git(repo, "add", "landed")
    _git(repo, "commit", "-q", "-m", "squash of the branch")
    _git(repo, "update-ref", "refs/remotes/origin/dev", "HEAD")
    return repo, head


def _merged_trailer_check(
    repo: Path, head: str, other: str
) -> subprocess.CompletedProcess[str]:
    value = _check_value("OMN-25198", _TRAILER_ITEM).replace(_NOT_ANCESTOR, other)
    view = json.dumps(
        {"verb": "pr-view", "result": {"state": "MERGED", "head_sha": head}}
    )
    return _run_with_sources(value, view=view, checks="{}", cwd=repo)


def test_the_trailer_check_reads_the_merged_range_of_the_head_sha(
    tmp_path: Path,
) -> None:
    repo, head = _merged_repo(tmp_path, [f"one\n\nTicket: OMN-25198\n{_FIX}"])
    other = _side_commit(repo)
    _git(repo, "switch", "-q", "landed")
    done = _merged_trailer_check(repo, head, other)
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_trailer_check_fails_a_merged_range_naming_two_fixes(
    tmp_path: Path,
) -> None:
    repo, head = _merged_repo(
        tmp_path,
        [
            f"one\n\nTicket: OMN-25198\n{_FIX}",
            "two\n\nBinds-Merged-Fix: OMN-21217 " + "1" * 40,
        ],
    )
    other = _side_commit(repo)
    _git(repo, "switch", "-q", "landed")
    done = _merged_trailer_check(repo, head, other)
    assert done.returncode == 1, done.stdout + done.stderr


def test_the_trailer_check_fails_when_the_merged_head_sha_is_not_in_the_clone(
    tmp_path: Path,
) -> None:
    repo, _ = _merged_repo(tmp_path, [f"one\n\nTicket: OMN-25198\n{_FIX}"])
    other = _side_commit(repo)
    _git(repo, "switch", "-q", "landed")
    done = _merged_trailer_check(repo, "0" * 40, other)
    # git answers 128 for the unknown object; the gate fails any non-zero status
    assert done.returncode != 0, done.stdout + done.stderr
    assert "Not a valid commit name" in done.stderr


# Finding 3: the gate overlays the head's tests onto the merge-base tree and a
# test-only diff takes TEST_ONLY_DIFF, so no "exit 5 at the merge base" control
# runs for the OMN-25286 and OMN-25316 contracts; their text must not claim one.
_CONTROL_CLAIM = re.compile(
    r"(fail(s|ing)?|exits? 5|exit 5)[^.;]*at the merge base|control[^.;]*at the merge base",
    re.IGNORECASE,
)


def _claim_texts(name: str) -> list[tuple[str, str]]:
    contract = _contract(name)
    texts = [
        (f"{r['id']}:{ac['id']}", ac["statement"])
        for r in contract["requirements"]
        for ac in r["acceptance"]
    ]
    texts += [(i["id"], i["description"]) for i in contract["dod_evidence"]]
    return texts


@pytest.mark.parametrize("name", ["OMN-25286", "OMN-25316"])
def test_the_criteria_claim_no_exit_5_control_at_the_merge_base(name: str) -> None:
    claims = [
        where for where, text in _claim_texts(name) if _CONTROL_CLAIM.search(text)
    ]
    assert claims == [], f"{name} claims a control at the merge base in {claims}"


def test_the_merge_base_item_of_omn25316_says_what_the_gate_does() -> None:
    text = _item("OMN-25316", "dod-omn25316-merge-base")["description"]
    assert "TEST_ONLY_DIFF" in text
    assert "overlay" in text


# Finding 2: a receipt may not say a log is attached to the pull request comment,
# which no test can read; each run names the host that ran it and the command.
def test_no_receipt_claims_a_log_attached_in_the_pull_request_comment() -> None:
    receipts = _contract("OMN-25198")["context"]["run_receipts"]
    texts = _strings(receipts)
    assert [t for t in texts if "attached in the pull request comment" in t] == []
    assert [t for t in texts if "are in the pull request comment" in t] == []
    assert [t for t in texts if "the logs are in" in t] == []


def test_the_omn25379_receipt_criterion_claims_no_attached_log() -> None:
    statements = [s for _, s in _claim_texts("OMN-25379")]
    assert [s for s in statements if "are in the Lab comment" in s] == []
