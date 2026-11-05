# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22497: the dev-tag train judges a tag on dev's required checks only.

Failure modes, listed before the tests were written:
1. a non-required red (omnimarket's Auto-Merge refusal, OMN-19032) marks the tag red;
2. a required red is dropped by the filter and the tag reads green;
3. a required check still running reads green;
4. no required check ran on the commit (they are pull-request only) and the tag reads green;
5. the required list is unreadable or empty and every tag reads green;
6. protection and rulesets each hold part of the required set and only one is read.
Also: the node's contract must bind the handler it routes to.
The gh answers are shaped on omnimarket dev tag 4f751fb962c1, read 2026-11-05:
32 required contexts, 6 of them run on the merge commit, and the only reds are
Enable Auto-Merge and Heal release-window and vendor-parity reds, neither required.
"""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dev_tag_pin_mover_compute import train_sources
from omnimarket.nodes.node_dev_tag_pin_mover_compute.handlers.handler_dev_tag_green_verdict import (
    HandlerDevTagGreenVerdict,
    required_checks_verdict,
)
from omnimarket.nodes.node_dev_tag_pin_mover_compute.models import (
    EnumDevTagGreenVerdict,
    ModelDevTagCheckRun,
    ModelDevTagGreenVerdictRequest,
)

_REQUIRED = frozenset(
    {"CI Summary", "Tests (Split 1/20)", "repo-evidence / dod-verify"}
)
_PROTECTION = "repos/OmniNode-ai/omnimarket/branches/dev"
_RULES = "repos/OmniNode-ai/omnimarket/rules/branches/dev"
_RUNS = "repos/OmniNode-ai/omnimarket/commits/"
_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_dev_tag_pin_mover_compute/contract.yaml"
)


def _fake_gh(answers: dict[str, str | None]):
    """A gh runner answering by API path; None for a path makes that read fail."""

    def run(argv, **_kwargs):
        path = argv[3]
        for key, out in answers.items():
            if path.startswith(key):
                if out is None:
                    return subprocess.CompletedProcess(argv, 1, "", "HTTP 404")
                return subprocess.CompletedProcess(argv, 0, out, "")
        raise AssertionError(f"unexpected gh call {path}")

    return run


def _answers(
    runs: str,
    protection: str | None = "CI Summary\nTests (Split 1/20)\n",
    rules: str | None = "repo-evidence / dod-verify\n",
) -> dict[str, str | None]:
    return {_PROTECTION: protection, _RULES: rules, _RUNS: runs}


def test_required_checks_only_a_non_required_red_reads_green() -> None:
    runs = [
        ("CI Summary", "completed", "success"),
        ("Tests (Split 1/20)", "completed", "success"),
        ("Enable Auto-Merge", "completed", "failure"),
        ("Heal release-window and vendor-parity reds", "completed", "failure"),
    ]
    assert required_checks_verdict(runs, _REQUIRED) is True


def test_required_checks_only_a_required_red_still_reads_red() -> None:
    runs = [
        ("CI Summary", "completed", "failure"),
        ("Enable Auto-Merge", "completed", "success"),
    ]
    assert required_checks_verdict(runs, _REQUIRED) is False
    assert (
        required_checks_verdict([("CI Summary", "completed", "cancelled")], _REQUIRED)
        is False
    )


def test_required_checks_only_a_running_required_check_reads_unread() -> None:
    runs = [
        ("CI Summary", "completed", "success"),
        ("Tests (Split 1/20)", "in_progress", ""),
    ]
    assert required_checks_verdict(runs, _REQUIRED) is None


def test_required_checks_only_no_required_run_on_the_commit_reads_unread() -> None:
    assert (
        required_checks_verdict(
            [("Enable Auto-Merge", "completed", "success")], _REQUIRED
        )
        is None
    )
    assert required_checks_verdict([], _REQUIRED) is None


@pytest.mark.parametrize("required", [None, frozenset()])
def test_required_checks_only_an_unreadable_required_list_reads_red(required) -> None:
    assert (
        required_checks_verdict([("CI Summary", "completed", "success")], required)
        is False
    )


def test_required_checks_only_protection_and_rulesets_are_both_read() -> None:
    assert (
        train_sources.required_contexts("omnimarket", run=_fake_gh(_answers("")))
        == _REQUIRED
    )


@pytest.mark.parametrize("broken", ["protection", "rules"])
def test_required_checks_only_a_failed_required_read_makes_the_tag_red(broken) -> None:
    runs = "CI Summary\tcompleted\tsuccess\nTests (Split 1/20)\tcompleted\tsuccess\n"
    answers = _answers(runs, **{broken: None})
    assert train_sources.required_contexts("omnimarket", run=_fake_gh(answers)) is None
    assert (
        train_sources.github_check_runs_green(
            "omnimarket", "4f751fb962c1", run=_fake_gh(answers)
        )
        is False
    )


def test_required_checks_only_end_to_end_on_the_2026_11_05_tag_shape() -> None:
    runs = (
        "CI Summary\tcompleted\tsuccess\n"
        "Tests (Split 1/20)\tcompleted\tsuccess\n"
        "Enable Auto-Merge\tcompleted\tfailure\n"
        "Heal release-window and vendor-parity reds\tcompleted\tfailure\n"
    )
    green = train_sources.github_check_runs_green(
        "omnimarket", "4f751fb962c1", run=_fake_gh(_answers(runs))
    )
    assert green is True
    red = runs.replace(
        "CI Summary\tcompleted\tsuccess", "CI Summary\tcompleted\tfailure"
    )
    assert (
        train_sources.github_check_runs_green(
            "omnimarket", "4f751fb962c1", run=_fake_gh(_answers(red))
        )
        is False
    )


def test_required_checks_only_unreadable_check_runs_read_unread() -> None:
    answers = _answers("")
    answers[_RUNS] = None
    assert (
        train_sources.github_check_runs_green(
            "omnimarket", "4f751fb962c1", run=_fake_gh(answers)
        )
        is None
    )


@pytest.mark.parametrize("hang_on", [_PROTECTION, _RUNS])
def test_required_checks_only_a_gh_read_that_hangs_never_reads_green(hang_on) -> None:
    runs = "CI Summary\tcompleted\tsuccess\n"
    answering = _fake_gh(_answers(runs))

    def run(argv, **kwargs):
        if argv[3].startswith(hang_on):
            raise subprocess.TimeoutExpired(argv, 60)
        return answering(argv, **kwargs)

    verdict = train_sources.github_check_runs_green(
        "omnimarket", "4f751fb962c1", run=run
    )
    assert verdict is (False if hang_on == _PROTECTION else None)


@pytest.mark.parametrize(
    ("status", "conclusion", "expected"),
    [
        ("completed", "success", EnumDevTagGreenVerdict.GREEN),
        ("completed", "failure", EnumDevTagGreenVerdict.RED),
        ("in_progress", "", EnumDevTagGreenVerdict.UNREAD),
    ],
)
def test_required_checks_only_the_handler_gives_the_same_verdict(
    status, conclusion, expected
) -> None:
    request = ModelDevTagGreenVerdictRequest(
        runs=(
            ModelDevTagCheckRun(
                name="CI Summary", status=status, conclusion=conclusion
            ),
            ModelDevTagCheckRun(
                name="Enable Auto-Merge", status="completed", conclusion="failure"
            ),
        ),
        required_contexts=_REQUIRED,
    )
    assert HandlerDevTagGreenVerdict().handle(request).verdict is expected


def test_required_checks_only_the_contract_routes_to_the_handler() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    routes = {
        entry["operation"]: entry["handler"]
        for entry in contract["handler_routing"]["handlers"]
    }
    handler = routes["judge_dev_tag_green"]
    module = importlib.import_module(handler["module"])
    assert getattr(module, handler["name"]) is HandlerDevTagGreenVerdict
