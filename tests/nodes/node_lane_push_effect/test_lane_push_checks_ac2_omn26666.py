# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26666 AC2: every declared check has a builder, and each builder runs its real check."""

from __future__ import annotations

import importlib
import uuid
from collections.abc import Sequence
from typing import Any

import pytest

MODELS = "omnimarket.nodes.node_lane_push_effect.models"
PROTOCOLS = "omnimarket.nodes.node_lane_push_effect.protocols"
HANDLER = "omnimarket.nodes.node_lane_push_effect.handlers.handler_lane_push"
CHECKS = "omnimarket.nodes.node_lane_push_effect.handlers.lane_push_checks"
HEAD = "a" * 40
COMMIT = "c" * 40


class _Runner:
    def __init__(self, exit_code: int = 0, output: str = "") -> None:
        self.exit_code = exit_code
        self.output = output
        self.seen: list[tuple[list[str], str]] = []

    def run(self, argv: Sequence[str], cwd: str, timeout_s: float) -> Any:
        self.seen.append((list(argv), cwd))
        return importlib.import_module(PROTOCOLS).CommandOutcome(
            exit_code=self.exit_code, output=self.output
        )


class _Reader:
    def __init__(self, state: Any) -> None:
        self.state = state
        self.lanes: list[str] = []

    def read(self, lane: str) -> Any:
        self.lanes.append(lane)
        return self.state


def _request(expected: str = HEAD) -> Any:
    models = importlib.import_module(MODELS)
    handler = importlib.import_module(HANDLER)
    repository = sorted(handler.declared_test_repositories())[0]
    return models.ModelLanePushRequest(
        correlation_id=uuid.uuid4(),
        lane="pushnode2-1915",
        repository=repository,
        branch="omn-26666-x",
        expected_remote_head=expected,
        source=models.ModelLanePushSource(
            kind="bundle", location="bundles/x.bundle", head_sha=COMMIT
        ),
    )


def _ok_state() -> Any:
    return importlib.import_module(MODELS).ModelLaneRulingState(claim_found=True)


def test_ac2_every_declared_check_id_has_a_builder_in_declared_order() -> None:
    ids = importlib.import_module(HANDLER).declared_check_ids()
    built = importlib.import_module(CHECKS).build_checks(
        ids, _Runner(), _Reader(_ok_state())
    )
    assert [c.check_id for c in built] == ids


def test_ac2_a_declared_check_with_no_builder_stops_the_build() -> None:
    with pytest.raises(ValueError, match="no builder for declared check made_up"):
        importlib.import_module(CHECKS).build_checks(
            ["made_up"], _Runner(), _Reader(_ok_state())
        )


def test_ac2_the_built_checks_make_a_handler_that_starts() -> None:
    handler_module = importlib.import_module(HANDLER)
    built = importlib.import_module(CHECKS).build_checks(
        handler_module.declared_check_ids(), _Runner(), _Reader(_ok_state())
    )
    parts: dict[str, Any] = dict.fromkeys(
        ("remote_head_reader", "source_materializer", "credential_issuer", "pusher")
    )
    handler_module.HandlerLanePushEffect(checks=built, **parts)


def test_ac2_a_pre_push_hook_runs_the_repositorys_own_hook_over_the_handed_commits() -> (
    None
):
    runner = _Runner()
    checks = importlib.import_module(CHECKS).build_checks(
        ["pre_push:mypy-type-check"], runner, _Reader(_ok_state())
    )
    verdict = checks[0].run(_request(), "/work/tree")
    assert verdict.passed
    assert runner.seen == [
        (
            [
                "pre-commit",
                "run",
                "mypy-type-check",
                "--hook-stage",
                "pre-push",
                "--from-ref",
                HEAD,
                "--to-ref",
                COMMIT,
            ],
            "/work/tree",
        )
    ]


def test_ac2_a_new_branch_runs_the_hook_over_all_files() -> None:
    runner = _Runner()
    checks = importlib.import_module(CHECKS).build_checks(
        ["pre_push:mypy-type-check"], runner, _Reader(_ok_state())
    )
    checks[0].run(_request(importlib.import_module(MODELS).NULL_HEAD), "/work/tree")
    assert "--all-files" in runner.seen[0][0]
    assert "--from-ref" not in runner.seen[0][0]


def test_ac2_a_hook_that_exits_non_zero_refuses_and_carries_its_output() -> None:
    runner = _Runner(exit_code=1, output="ticket contract missing")
    checks = importlib.import_module(CHECKS).build_checks(
        ["pre_push:ticket-contract-prerequisite"], runner, _Reader(_ok_state())
    )
    verdict = checks[0].run(_request(), "/work/tree")
    assert not verdict.passed
    assert "ticket contract missing" in verdict.finding


def test_ac2_release_identity_runs_the_commit_stage_hook() -> None:
    runner = _Runner()
    checks = importlib.import_module(CHECKS).build_checks(
        ["release_identity"], runner, _Reader(_ok_state())
    )
    checks[0].run(_request(), "/work/tree")
    argv = runner.seen[0][0]
    assert argv[:3] == ["pre-commit", "run", "check-release-identity"]
    assert argv[argv.index("--hook-stage") + 1] == "pre-commit"


def test_ac2_ruling_guard_reads_the_lanes_claim_from_the_reader() -> None:
    models = importlib.import_module(MODELS)
    reader = _Reader(models.ModelLaneRulingState(claim_found=True))
    check = importlib.import_module(CHECKS).build_checks(
        ["ruling_guard"], _Runner(), reader
    )[0]
    assert check.run(_request(), "/work/tree").passed
    assert reader.lanes == ["pushnode2-1915"]


def test_ac2_ruling_guard_refuses_a_lane_with_no_claim() -> None:
    models = importlib.import_module(MODELS)
    check = importlib.import_module(CHECKS).build_checks(
        ["ruling_guard"],
        _Runner(),
        _Reader(models.ModelLaneRulingState(claim_found=False)),
    )[0]
    verdict = check.run(_request(), "/work/tree")
    assert not verdict.passed
    assert "no CLAIM" in verdict.finding


def test_ac2_ruling_guard_refuses_a_ruling_with_no_ack() -> None:
    models = importlib.import_module(MODELS)
    state = models.ModelLaneRulingState(
        claim_found=True, unacknowledged_rulings=("2026-11-24T16:38:37Z",)
    )
    check = importlib.import_module(CHECKS).build_checks(
        ["ruling_guard"], _Runner(), _Reader(state)
    )[0]
    verdict = check.run(_request(), "/work/tree")
    assert not verdict.passed
    assert "2026-11-24T16:38:37Z" in verdict.finding
