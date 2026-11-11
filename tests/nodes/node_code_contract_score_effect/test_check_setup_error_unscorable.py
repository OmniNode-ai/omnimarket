# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23637 D2: a check that ends in a pytest setup error with no failed test is unscorable, never a fail.

By the node's own rule an infrastructure fault at check setup (a fixture that needs a service that is not there) is
``unscorable``. A test that fails is still ``fail_check``, and so is a collection error (pytest exit 2: the check cannot
import what the draft was meant to add, which is the normal failing shape of a draft that is wrong or empty).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from omnimarket.nodes.node_code_contract_score_effect.handlers.adapter_git_workspace import (
    BashCheckExecution,
)
from omnimarket.nodes.node_code_contract_score_effect.handlers.handler_code_contract_score import (
    HandlerCodeContractScore,
)
from omnimarket.nodes.node_code_contract_score_effect.models import (
    EnumCodeScoreSource,
    EnumCodeScoreVerdict,
    ModelCodeScored,
)
from omnimarket.nodes.node_code_contract_score_effect.protocols import RunOutcome
from tests.nodes.node_code_contract_score_effect.fixture_repo import (
    CHECK_PATH,
    build_repo,
    contract_text,
    request,
)

pytestmark = pytest.mark.unit

SCORED_TOPIC = "onex.evt.omnimarket.code-contract-scored.v1"

# A fixture that raises at setup, as one that cannot reach Postgres does. The command writes it beside the placed check.
_SETUP_ERROR_COMMAND = (
    "printf 'import pytest\\n\\n\\n@pytest.fixture(autouse=True)\\n"
    "def _service():\\n    raise ConnectionRefusedError\\n' > conftest.py"
    f" && {sys.executable} -m pytest -q -p no:cacheprovider {CHECK_PATH}"
)
_SETUP_ERROR_TAIL = (
    "ERROR tests/test_add.py::test_add - RuntimeError: connection refused\n"
    "1 error in 2.62s"
)
_COLLECTION_ERROR_TAIL = (
    "ERROR tests/test_add.py\n"
    "!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!\n"
    "1 error in 0.13s"
)
_FAILED_AND_ERROR_TAIL = (
    "FAILED tests/test_add.py::test_add - assert 1 == 2\n"
    "ERROR tests/test_add.py::test_other - RuntimeError: connection refused\n"
    "1 failed, 1 error in 0.40s"
)


class CannedRunner:
    """Runs the env sync as true, and answers every check with one canned outcome."""

    def __init__(self, outcome: RunOutcome) -> None:
        self._outcome = outcome
        self._real = BashCheckExecution()

    def run(
        self, command: str, cwd: Path, timeout_s: float, tail_chars: int
    ) -> RunOutcome:
        if command == "true":
            return self._real.run(command, cwd, timeout_s, tail_chars)
        return self._outcome


class FakeBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes | None, bytes]] = []

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> None:
        self.published.append((topic, key, value))


async def verdict_for(runner: CannedRunner, fx):
    result = await HandlerCodeContractScore(runner=runner).handle(
        request(fx, fx.good_diff)
    )
    assert result.scored is not None, result.failed
    return result.scored.verdict


async def test_a_real_pytest_setup_error_is_unscorable_not_fail_check(
    tmp_path: Path,
) -> None:
    """The failing control of D2: red on the handler that reads the exit code only (it says fail_check)."""
    text = contract_text(command=_SETUP_ERROR_COMMAND)
    built = build_repo(tmp_path, contract=text)
    result = await HandlerCodeContractScore().handle(
        request(built, built.good_diff, contract=text)
    )
    assert result.scored is not None, result.failed
    verdict = result.scored.verdict
    assert verdict.checks is EnumCodeScoreVerdict.UNSCORABLE
    assert not verdict.checks.is_fail
    assert verdict.source is EnumCodeScoreSource.ENV
    assert "1 error" in verdict.detail


async def test_golden_chain_setup_error_publishes_one_unscorable_scored_event(
    tmp_path: Path,
) -> None:
    text = contract_text(command=_SETUP_ERROR_COMMAND)
    built = build_repo(tmp_path, contract=text)
    bus = FakeBus()
    result = await HandlerCodeContractScore(event_bus=bus).handle(
        request(built, built.good_diff, contract=text)
    )
    assert [topic for topic, _, _ in bus.published] == [SCORED_TOPIC]
    scored = ModelCodeScored.model_validate_json(bus.published[0][2])
    assert scored == result.scored
    assert scored.verdict.checks is EnumCodeScoreVerdict.UNSCORABLE
    assert scored.verdict.source is EnumCodeScoreSource.ENV


async def test_a_pytest_setup_error_with_the_canned_tail_is_unscorable_env(fx) -> None:
    verdict = await verdict_for(
        CannedRunner(RunOutcome(1, False, 0.1, _SETUP_ERROR_TAIL)), fx
    )
    assert verdict.checks is EnumCodeScoreVerdict.UNSCORABLE
    assert verdict.source is EnumCodeScoreSource.ENV


async def test_error_chain_a_failed_test_beside_an_error_is_still_fail_check(
    fx,
) -> None:
    verdict = await verdict_for(
        CannedRunner(RunOutcome(1, False, 0.1, _FAILED_AND_ERROR_TAIL)), fx
    )
    assert verdict.checks is EnumCodeScoreVerdict.FAIL_CHECK
    assert verdict.source is None


async def test_error_chain_a_collection_error_is_still_fail_check(fx) -> None:
    verdict = await verdict_for(
        CannedRunner(RunOutcome(2, False, 0.1, _COLLECTION_ERROR_TAIL)), fx
    )
    assert verdict.checks is EnumCodeScoreVerdict.FAIL_CHECK


async def test_error_chain_a_non_pytest_command_failing_is_still_fail_check(
    fx,
) -> None:
    verdict = await verdict_for(
        CannedRunner(RunOutcome(1, False, 0.1, "make: *** [all] Error 1")), fx
    )
    assert verdict.checks is EnumCodeScoreVerdict.FAIL_CHECK


async def test_error_chain_a_failed_test_is_still_fail_check_end_to_end(fx) -> None:
    result = await HandlerCodeContractScore().handle(request(fx, ""))
    assert result.scored is not None
    assert result.scored.verdict.checks is EnumCodeScoreVerdict.FAIL_CHECK


def test_summary_parser_reads_the_closing_line_only() -> None:
    from omnimarket.nodes.node_code_contract_score_effect.handlers.parser_pytest_summary import (
        is_setup_error_run,
        parse_pytest_summary,
    )

    assert parse_pytest_summary("noise\n=== 3 passed, 1 error in 2.62s ===").errors == 1
    assert parse_pytest_summary("1 failed, 1 error in 0.40s").failed == 1
    assert parse_pytest_summary("12 passed in 1.00s (0:00:01)").passed == 12
    assert parse_pytest_summary("an error occurred in 3s of work") is None
    assert parse_pytest_summary("") is None
    assert is_setup_error_run(1, "1 error in 0.03s")
    assert not is_setup_error_run(2, "1 error in 0.03s")
    assert not is_setup_error_run(1, "1 failed, 1 error in 0.03s")
    assert not is_setup_error_run(1, "1 failed in 0.03s")
    assert not is_setup_error_run(None, "1 error in 0.03s")
