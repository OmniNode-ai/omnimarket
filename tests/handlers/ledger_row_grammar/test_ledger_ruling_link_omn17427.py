# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# Rewrote test_onex_ledger_refuses_and_writes_nothing; dropped retired append command I/O.
"""Ruling links require production mechanisms and operator-backed demotions."""

from __future__ import annotations

import pytest

from omnimarket.handlers import ledger_row_grammar as g

pytestmark = pytest.mark.unit
RULING_TS = "2026-10-10T11:04:11Z"
STAMP = "2026-10-17T17:00:00Z"
HEAD = f"{STAMP} | CORRECTION | lane=lane-a | ticket=OMN-17427 | corrects={RULING_TS}"


@pytest.mark.parametrize(
    "path",
    [
        "repo/tests/test_feature.py",
        "repo-private-data/tests/nodes/test_worker.py",
        "repo-plugin/plugins/omni/skills/operator-ruling/tests/test_decision.py",
        "repo-private-data/conftest.py",
        "repo-infra/scripts/check_thing_test.sh",
    ],
)
def test_test_file_mechanism_refused(path: str) -> None:
    reasons = g.row_refusals(f"{HEAD} | mechanism={path} | why")
    assert any("is a test file" in r and path in r for r in reasons), reasons


def test_test_file_refused_inside_a_list() -> None:
    script = "repo-private-data/.github/workflows/ci.yml"
    test = "repo-private-data/tests/test_question.py"
    reasons = g.row_refusals(f"{HEAD} | mechanism={script},{test} | why")
    assert [r for r in reasons if "is a test file" in r] == [
        r for r in reasons if test in r and "is a test file" in r
    ]
    assert any(test in r for r in reasons)
    assert not any(script in r for r in reasons)


@pytest.mark.parametrize(
    "path",
    [
        "repo-private-data/.github/workflows/ci.yml",
        "repo-plugin/plugins/onex/hooks/scripts/pre_tool_use_bash_guards.sh",
        "repo-private-data/src/repo-private-data/ledger/ledger_grammar.py",
        "repo-private-data/launchd/landing-controller/test.example.worker.plist.tmpl",
        "repo-core/src/repo-core/validators/contest_shape.py",
    ],
)
def test_non_test_mechanism_passes(path: str) -> None:
    assert g.row_refusals(f"{HEAD} | mechanism={path} | why") == []


@pytest.mark.parametrize("row_type", ["CORRECTION", "STATUS", "TERMINAL", "ACK"])
def test_decision_demotion_without_operator_row_refused(row_type: str) -> None:
    line = f"{STAMP} | {row_type} | lane=lane-a | corrects={RULING_TS} | kind=decision | why"
    reasons = g.ruling_link_refusals(g.parse_row(line))
    assert any("operator-row=" in r for r in reasons), reasons


def test_decision_demotion_with_operator_row_passes() -> None:
    line = f"{HEAD} | kind=decision | operator-row=2026-10-17T09:03:06Z | why"
    assert g.row_refusals(line) == []


def test_decision_with_mechanism_is_not_a_demotion() -> None:
    line = f"{HEAD} | kind=decision | mechanism-ticket=OMN-17427 | why"
    assert g.ruling_link_refusals(g.parse_row(line)) == []


def test_decision_without_a_link_is_not_a_demotion() -> None:
    line = (
        f"{STAMP} | CORRECTION | lane=lane-a | ticket=OMN-17427 | kind=decision | why"
    )
    assert g.ruling_link_refusals(g.parse_row(line)) == []


def test_ruling_row_kind_decision_passes() -> None:
    line = (
        f"{STAMP} | RULING | lane=lane-a | ticket=OMN-17427 | question=one-off? | "
        f'kind=decision | re={RULING_TS}-lane-b | "yes"'
    )
    assert g.ruling_link_refusals(g.parse_row(line)) == []


def test_payload_refuses_a_test_mechanism_and_accepts_its_workflow() -> None:
    ledger = "ROLLING_WORK_LEDGER.md"
    bad = f"{HEAD} | mechanism=repo/tests/test_x.py | why"
    refused = g.refusal_for_payload(bad, ledger)
    assert refused is not None
    assert "is a test file" in refused
    good = f"{HEAD} | mechanism=repo/.github/workflows/ci.yml | why"
    assert g.refusal_for_payload(good, ledger) is None
