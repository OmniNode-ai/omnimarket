# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3 of OMN-23626: an infrastructure fault is unscorable with its own source, never a fail; a refusal ends on the failed topic."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_code_contract_score_effect.handlers.adapter_git_workspace import (
    GitWorkspace,
)
from omnimarket.nodes.node_code_contract_score_effect.handlers.handler_code_contract_score import (
    HandlerCodeContractScore,
)
from omnimarket.nodes.node_code_contract_score_effect.models import (
    EnumCodeScoreFailureCode,
    EnumCodeScoreSource,
    EnumCodeScoreVerdict,
    ModelCodeScoreRequest,
)
from omnimarket.nodes.node_code_contract_score_effect.protocols import (
    RunOutcome,
    WorkspaceError,
)
from tests.nodes.node_code_contract_score_effect.fixture_repo import (
    Fixture,
    build_repo,
    contract_text,
    request,
)

pytestmark = pytest.mark.unit

FAILED_TOPIC = "onex.evt.omnimarket.code-contract-score-failed.v1"


class FakeBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes | None, bytes]] = []

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> None:
        self.published.append((topic, key, value))


async def unscorable(
    handler: HandlerCodeContractScore, req: ModelCodeScoreRequest
) -> EnumCodeScoreSource:
    result = await handler.handle(req)
    assert result.scored is not None, result.failed
    verdict = result.scored.verdict
    assert verdict.checks is EnumCodeScoreVerdict.UNSCORABLE
    assert not verdict.checks.is_fail
    assert verdict.source is not None
    return verdict.source


async def test_environment_sync_failure_is_unscorable_env(fx: Fixture) -> None:
    req = request(fx, fx.good_diff, overlay_over={"env_sync_command": "exit 3"})
    assert await unscorable(HandlerCodeContractScore(), req) is EnumCodeScoreSource.ENV


async def test_environment_sync_timeout_is_unscorable_env(fx: Fixture) -> None:
    req = request(
        fx,
        fx.good_diff,
        overlay_over={"env_sync_command": "sleep 5", "env_sync_timeout_s": 0.2},
    )
    assert await unscorable(HandlerCodeContractScore(), req) is EnumCodeScoreSource.ENV


async def test_unreadable_contract_is_unscorable_contract(tmp_path: Path) -> None:
    text = "ticket_id: [unclosed\n"
    built = build_repo(tmp_path, contract=text)
    req = request(built, built.good_diff, contract=text)
    assert (
        await unscorable(HandlerCodeContractScore(), req)
        is EnumCodeScoreSource.CONTRACT
    )


async def test_unknown_item_id_is_unscorable_contract(fx: Fixture) -> None:
    req = request(fx, fx.good_diff, item_ids=("dod-404",))
    assert (
        await unscorable(HandlerCodeContractScore(), req)
        is EnumCodeScoreSource.CONTRACT
    )


async def test_check_type_not_runnable_in_a_worktree_is_unscorable(
    tmp_path: Path,
) -> None:
    text = contract_text(check_type="command").replace(
        'check_type: command\n    check_value: "',
        'check_type: grep\n    check_value: "',
    )
    text = text.replace(
        text[text.index("check_value:") :],
        "check_value:\n      pattern: add\n      path: calc.py\n    cwd: .\n",
    )
    built = build_repo(tmp_path, contract=text)
    req = request(built, built.good_diff, contract=text)
    assert (
        await unscorable(HandlerCodeContractScore(), req)
        is EnumCodeScoreSource.CHECK_NOT_RUNNABLE
    )


async def test_worktree_failure_is_unscorable_worktree(fx: Fixture) -> None:
    class NoWorktree(GitWorkspace):
        def create(self, repo_root: str, base_commit: str, scratch_root: str) -> Path:
            raise WorkspaceError("disk full")

    source = await unscorable(
        HandlerCodeContractScore(workspace=NoWorktree()), request(fx, fx.good_diff)
    )
    assert source is EnumCodeScoreSource.WORKTREE


async def test_check_hitting_its_limit_is_unscorable_harness(fx: Fixture) -> None:
    class Slow:
        def run(self, command: str, cwd: Path, timeout_s: float) -> RunOutcome:
            if command == "true":
                return RunOutcome(0, False, 0.0, "")
            return RunOutcome(None, True, timeout_s, "")

    source = await unscorable(
        HandlerCodeContractScore(runner=Slow()), request(fx, fx.good_diff)
    )
    assert source is EnumCodeScoreSource.HARNESS


async def test_refused_request_ends_on_the_failed_topic_with_a_typed_code(
    fx: Fixture,
) -> None:
    bus = FakeBus()
    result = await HandlerCodeContractScore(event_bus=bus).handle(
        request(fx, fx.good_diff, check_sha="f" * 64)
    )
    assert result.scored is None
    assert result.failed is not None
    assert result.failed.code is EnumCodeScoreFailureCode.CHECK_FILE_SHA256_MISMATCH
    assert [topic for topic, _, _ in bus.published] == [FAILED_TOPIC]


async def test_internal_error_ends_on_the_failed_topic_never_a_verdict(
    fx: Fixture,
) -> None:
    class Exploding(GitWorkspace):
        def read_blob(self, repo_root: str, revision: str, path: str) -> bytes | None:
            raise OSError("clone vanished")

    bus = FakeBus()
    result = await HandlerCodeContractScore(
        workspace=Exploding(), event_bus=bus
    ).handle(request(fx, fx.good_diff))
    assert result.scored is None
    assert result.failed is not None
    assert result.failed.code is EnumCodeScoreFailureCode.INTERNAL_ERROR
    assert [topic for topic, _, _ in bus.published] == [FAILED_TOPIC]
