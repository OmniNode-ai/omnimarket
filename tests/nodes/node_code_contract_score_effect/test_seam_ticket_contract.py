# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam of OMN-23626: omnibase_core's ModelTicketContract and its dod_evidence checks (the contract the scorer reads).

Real contracts of the repository (the three rows of the first task set) are read through the scorer's own contract
reading. The scorer runs exactly the bound commands of the chosen items, skips an item bound to the local Done gate, and
refuses an item id the contract does not have. The test fails when omnibase_core changes the check or the scope shape.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
import yaml
from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract

from omnimarket.nodes.node_code_contract_score_effect.handlers.handler_code_contract_score import (
    HandlerCodeContractScore,
    sha256_hex,
)
from omnimarket.nodes.node_code_contract_score_effect.models import (
    EnumCodeScoreSource,
    EnumCodeScoreVerdict,
    ModelCodeScoreContractRef,
    ModelCodeScoreRequest,
    ModelCodeScoreTry,
)
from omnimarket.nodes.node_code_contract_score_effect.protocols import RunOutcome
from tests.nodes.node_code_contract_score_effect.fixture_repo import (
    REPO,
    Fixture,
    overlay,
)

pytestmark = pytest.mark.unit

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
ROWS = {
    "OMN-20804": "dod-omn20804-glob-is-not-an-install-path",
    "OMN-23346": "dod-omn23346-ac1-exclusion-dropped",
    "OMN-20413": "dod-omn20413-reader-coverage-opt-outs",
}
SHA = "a" * 40


class FakeWorkspace:
    def __init__(self, contract_text: bytes, scratch: Path) -> None:
        self._text = contract_text
        self._scratch = scratch

    def read_blob(self, repo_root: str, revision: str, path: str) -> bytes | None:
        return self._text

    def create(self, repo_root: str, base_commit: str, scratch_root: str) -> Path:
        self._scratch.mkdir(parents=True, exist_ok=True)
        return self._scratch

    def apply_diff(self, worktree: Path, diff: str) -> str | None:
        return None

    def place_file(self, worktree: Path, path: str, data: bytes) -> None:
        return None

    def remove(self, repo_root: str, worktree: Path) -> None:
        return None


class RecordingRunner:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, command: str, cwd: Path, timeout_s: float) -> RunOutcome:
        self.commands.append(command)
        return RunOutcome(1 if command != "true" else 0, False, 0.0, "")


def _request(
    fx: Fixture, text: bytes, ticket: str, item_ids: tuple[str, ...]
) -> ModelCodeScoreRequest:
    return ModelCodeScoreRequest(
        correlation_id=UUID("22222222-2222-4222-8222-222222222222"),
        repo=REPO,
        base_commit=SHA,
        draft_diff="",
        contract=ModelCodeScoreContractRef(
            repository=REPO,
            path=f"contracts/{ticket}.yaml",
            revision=SHA,
            sha256=sha256_hex(text),
        ),
        check_item_ids=item_ids,
        try_identity=ModelCodeScoreTry(task_key=ticket),
        overlay=overlay(fx),
    )


@pytest.mark.parametrize(("ticket", "item_id"), sorted(ROWS.items()))
async def test_real_contract_binds_the_checks_the_scorer_runs(
    fx: Fixture, tmp_path: Path, ticket: str, item_id: str
) -> None:
    path = CONTRACTS / f"{ticket}.yaml"
    text = path.read_bytes()
    contract = ModelTicketContract.model_validate(yaml.safe_load(text))
    item = next(i for i in contract.dod_evidence if i.id == item_id)
    expected = [
        c.check_value
        for c in item.checks
        if c.check_type.value in ("test_passes", "command")
    ]
    assert expected, "the row's item binds a runnable check"

    runner = RecordingRunner()
    result = await HandlerCodeContractScore(
        workspace=FakeWorkspace(text, tmp_path / "wt"), runner=runner
    ).handle(_request(fx, text, ticket, (item_id,)))
    assert result.scored is not None
    assert result.scored.verdict.checks is EnumCodeScoreVerdict.FAIL_CHECK
    assert runner.commands == ["true", *expected]


async def test_a_local_done_gate_item_is_never_run(fx: Fixture, tmp_path: Path) -> None:
    text = (CONTRACTS / "OMN-23346.yaml").read_bytes()
    runner = RecordingRunner()
    await HandlerCodeContractScore(
        workspace=FakeWorkspace(text, tmp_path / "wt"), runner=runner
    ).handle(_request(fx, text, "OMN-23346", ()))
    assert not any("lab-work run" in command for command in runner.commands)
    assert any(
        "check_unimported_handlers.py --transport-imports" in command
        for command in runner.commands
    )


async def test_an_item_id_the_contract_does_not_have_is_unscorable_contract(
    fx: Fixture, tmp_path: Path
) -> None:
    text = (CONTRACTS / "OMN-23346.yaml").read_bytes()
    result = await HandlerCodeContractScore(
        workspace=FakeWorkspace(text, tmp_path / "wt"), runner=RecordingRunner()
    ).handle(_request(fx, text, "OMN-23346", ("dod-not-there",)))
    assert result.scored is not None
    assert result.scored.verdict.source is EnumCodeScoreSource.CONTRACT
