# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 of OMN-23626: verdicts over a fixture repository."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_code_contract_score_effect.handlers.handler_code_contract_score import (
    HandlerCodeContractScore,
)
from omnimarket.nodes.node_code_contract_score_effect.models import (
    EnumCodeScoreSource,
    EnumCodeScoreVerdict,
)
from tests.nodes.node_code_contract_score_effect.fixture_repo import (
    CHECK_PATH,
    CONTRACT_PATH,
    SOURCE_PATH,
    Fixture,
    edit_diff,
    request,
)

pytestmark = pytest.mark.unit


async def score(fx: Fixture, diff: str):
    result = await HandlerCodeContractScore().handle(request(fx, diff))
    assert result.scored is not None, result.failed
    return result.scored.verdict


async def test_empty_draft_fails_check(fx: Fixture) -> None:
    verdict = await score(fx, "")
    assert verdict.checks is EnumCodeScoreVerdict.FAIL_CHECK


async def test_known_good_diff_passes(fx: Fixture) -> None:
    verdict = await score(fx, fx.good_diff)
    assert verdict.checks is EnumCodeScoreVerdict.PASS
    assert [r.exit_code for r in verdict.check_results] == [0]


async def test_diff_that_does_not_apply_is_fail_did_not_apply(fx: Fixture) -> None:
    broken = fx.good_diff.replace("return a - b", "return a * b")
    verdict = await score(fx, broken)
    assert verdict.checks is EnumCodeScoreVerdict.FAIL_DID_NOT_APPLY


async def test_diff_editing_the_check_file_is_fail_tampered(fx: Fixture) -> None:
    diff = edit_diff(fx, CHECK_PATH, "", "def test_add():\n    assert True\n")
    verdict = await score(fx, diff)
    assert verdict.checks is EnumCodeScoreVerdict.FAIL_TAMPERED


async def test_diff_editing_the_contract_is_fail_tampered(fx: Fixture) -> None:
    diff = edit_diff(fx, CONTRACT_PATH, "", "schema_version: 1.0.0\n")
    verdict = await score(fx, diff)
    assert verdict.checks is EnumCodeScoreVerdict.FAIL_TAMPERED


async def test_diff_editing_a_protected_path_is_fail_tampered(fx: Fixture) -> None:
    diff = edit_diff(fx, ".github/workflows/x.yml", "", "on: push\n")
    verdict = await score(fx, diff)
    assert verdict.checks is EnumCodeScoreVerdict.FAIL_TAMPERED


async def test_a_wrong_but_applying_diff_is_fail_check_not_pass(fx: Fixture) -> None:
    wrong = edit_diff(fx, SOURCE_PATH, "", "def add(a, b):\n    return 0\n")
    verdict = await score(fx, wrong)
    assert verdict.checks is EnumCodeScoreVerdict.FAIL_CHECK
    assert verdict.source is None


async def test_gate_acceptance_is_recorded_beside_and_never_the_score(
    fx: Fixture,
) -> None:
    result = await HandlerCodeContractScore().handle(request(fx, ""))
    assert result.scored is not None
    assert result.scored.gate_accepted is True
    assert result.scored.verdict.checks is EnumCodeScoreVerdict.FAIL_CHECK


async def test_checks_that_pass_at_the_base_are_an_invalid_falsifier(tmp_path) -> None:
    from tests.nodes.node_code_contract_score_effect.fixture_repo import (
        build_repo,
        contract_text,
    )

    text = contract_text(command="true")
    built = build_repo(tmp_path, contract=text)
    result = await HandlerCodeContractScore().handle(request(built, "", contract=text))
    assert result.scored is not None
    assert result.scored.verdict.checks is EnumCodeScoreVerdict.UNSCORABLE
    assert result.scored.verdict.source is EnumCodeScoreSource.FALSIFIER_NOT_HELD
