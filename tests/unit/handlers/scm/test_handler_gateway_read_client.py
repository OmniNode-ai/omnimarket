# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-21146 -- the gateway read client's bus route.

The request is published on node_github_repo_gateway_effect's command topic and
the typed result is taken from the terminal event that names it; an answer that
never comes is a typed refusal, never a ``gh`` read.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from omnimarket.delegated_test_loop import lane_bus
from omnimarket.handlers.scm.handler_gateway_read_client import (
    GatewayReadError,
    read_over_bus,
)
from omnimarket.nodes.node_github_repo_gateway_effect.models.model_gateway_io import (
    EnumGithubGatewayOperation,
    ModelCommitCheckRunsResult,
    ModelGithubGatewayRequest,
    ModelMergedPrResult,
)
from tests.unit.handlers.scm.test_onex_scm_command import GATEWAY, HEAD, MOVED, FakeBus

pytestmark = pytest.mark.unit

REPO = "OmniNode-ai/omnimarket"


def _install(monkeypatch: pytest.MonkeyPatch, bus: FakeBus) -> list[str | None]:
    lanes: list[str | None] = []

    @contextlib.asynccontextmanager
    async def opener(
        *, bus: str, lane: str | None, kafka_bootstrap: str | None, root: Path | None
    ) -> AsyncIterator[FakeBus]:
        lanes.append(lane)
        yield fake

    fake = bus
    monkeypatch.setattr(lane_bus, "open_lab_run_bus", opener)
    return lanes


def test_merged_pr_is_published_to_the_gateway_and_its_result_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = FakeBus()
    lanes = _install(monkeypatch, bus)
    monkeypatch.delenv("ONEX_GITHUB_GATEWAY_BUS_LANE", raising=False)

    result = read_over_bus(
        ModelGithubGatewayRequest(
            operation=EnumGithubGatewayOperation.MERGED_PR, repo=REPO, pr_number=77
        )
    )

    assert isinstance(result, ModelMergedPrResult)
    assert (result.head_sha, result.merge_commit_sha) == (HEAD, MOVED)
    assert [topic for topic, _ in bus.published] == [GATEWAY.command]
    assert bus.published[0][1]["payload"]["operation"] == "merged_pr"
    assert lanes == ["dev"]


def test_commit_check_runs_over_the_bus_on_a_named_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = FakeBus()
    lanes = _install(monkeypatch, bus)
    monkeypatch.setenv("ONEX_GITHUB_GATEWAY_BUS_LANE", "lab-b")

    result = read_over_bus(
        ModelGithubGatewayRequest(
            operation=EnumGithubGatewayOperation.COMMIT_CHECK_RUNS,
            repo=REPO,
            head_sha=HEAD,
            check_name="repo-evidence / dod-verify",
        )
    )

    assert isinstance(result, ModelCommitCheckRunsResult)
    assert [run.name for run in result.check_runs] == ["repo-evidence / dod-verify"]
    assert lanes == ["lab-b"]


def test_no_answer_is_a_typed_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, FakeBus(answer=False))

    with pytest.raises(GatewayReadError, match="no answer on lane"):
        read_over_bus(
            ModelGithubGatewayRequest(
                operation=EnumGithubGatewayOperation.MERGED_PR,
                repo=REPO,
                pr_number=77,
            ),
            timeout_s=0.2,
        )
