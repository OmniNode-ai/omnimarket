# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24173 AC3: the score keeps each check's whole output; the 400-character cut is gone from the scorer.

The trial driver's trace (``node_code_eval_trial_orchestrator``) is built from what the scored event carries, so a
check that prints a long output must reach the event whole, in the runner and in the handler's result.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from omnimarket.models.model_code_score import ModelCodeScoreOverlay
from omnimarket.nodes.node_code_contract_score_effect.handlers.adapter_git_workspace import (
    BashCheckExecution,
)
from omnimarket.nodes.node_code_contract_score_effect.handlers.handler_code_contract_score import (
    HandlerCodeContractScore,
)
from omnimarket.nodes.node_code_contract_score_effect.protocols import RunOutcome
from tests.nodes.node_code_contract_score_effect.fixture_repo import request

pytestmark = pytest.mark.unit

THREE_MB = 3_000_000


def test_the_runner_returns_the_whole_output_of_a_three_megabyte_command(
    tmp_path: Path,
) -> None:
    command = f"{sys.executable} -c \"print('x' * {THREE_MB - 1})\""
    ran = BashCheckExecution().run(command, tmp_path, 60)
    assert ran.exit_code == 0
    assert len(ran.output) == THREE_MB  # the printed line and its newline
    assert ran.output.endswith("x\n")


def test_the_runner_keeps_what_a_timed_out_command_printed(tmp_path: Path) -> None:
    command = f"{sys.executable} -u -c \"import time; print('before'); time.sleep(30)\""
    ran = BashCheckExecution().run(command, tmp_path, 1)
    assert ran.timed_out
    assert "before" in ran.output


class OneBigCheck:
    """The env sync runs for real; the contract's check answers one three-megabyte output."""

    def __init__(self) -> None:
        self._real = BashCheckExecution()
        self.output = ("y" * 99 + "\n") * 30_000

    def run(self, command: str, cwd: Path, timeout_s: float) -> RunOutcome:
        if command == "true":
            return self._real.run(command, cwd, timeout_s)
        return RunOutcome(1, False, 0.1, self.output)


async def test_the_scored_event_carries_each_checks_whole_output(fx) -> None:
    runner = OneBigCheck()
    result = await HandlerCodeContractScore(runner=runner).handle(
        request(fx, fx.good_diff)
    )
    assert result.scored is not None, result.failed
    outputs = [c.output for c in result.scored.verdict.check_results]
    assert outputs == [runner.output]
    assert len(outputs[0]) == 3_000_000


def test_the_overlay_has_no_output_cut_and_still_loads_an_overlay_that_names_one() -> (
    None
):
    assert "output_tail_chars" not in ModelCodeScoreOverlay.model_fields
    values: dict[str, object] = {
        "check_timeout_s": 60,
        "env_sync_timeout_s": 60,
        "runnable_check_types": ("command",),
        "env_sync_command": "true",
        "scratch_root": "/scratch/example",
        "repo_roots": {"example/repo": "/srv/example/repo"},
        "protected_paths": {"example/repo": ("contracts/",)},
        "output_tail_chars": 400,  # an overlay written before the cut was retired
    }
    assert ModelCodeScoreOverlay.model_validate(values).check_timeout_s == 60
