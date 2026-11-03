# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20070: repo-owned acceptance evidence and its base-branch caller."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml

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
        # OMN-22403: the hosted receipt gate runs a local_done_gate item neither at
        # the head nor at the merge base, so it has no control and refuses a bound
        # one. An empty binds_ac binds nothing and is the dev convention.
        if item.get("execution_scope") == "local_done_gate" and item["binds_ac"]:
            violations.append(
                f"{label}: a local_done_gate item may not carry binds_ac; "
                "scoped local_done_gate, which this hosted check runs neither at "
                "the head nor at the merge base"
            )
            continue
        bound.update(item["binds_ac"])
        argvs = [
            _pytest_argv(check.get("check_value", ""))
            for check in item.get("checks", [])
            if check.get("check_type") == "test_passes"
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
