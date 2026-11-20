# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The GitHub gateway as the scheduled closer's open-pull-request source (OMN-25459).

The closer reads its open pull requests through ``node_github_repo_gateway_effect`` over the
bus, one ``open_pr_facts`` read per scope repository, and matches a ticket id in a pull
request's title or head branch exactly as the watcher state did. A read that fails, answers
the wrong result type or is cut reads the open set as unread, never as an empty set.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.scm.handler_gateway_read_client import GatewayReadError
from omnimarket.models.closeout_scope_overlay import ModelCloseoutScopeOverlay
from omnimarket.models.model_github_gateway_io import (
    EnumGithubGatewayOperation,
    GithubGatewayResult,
    ModelGithubGatewayRequest,
    ModelOpenPrFact,
    ModelOpenPrFactsResult,
    ModelOpenPrsResult,
)
from omnimarket.models.ticket_closer import (
    EnumCloserReadingSource,
    ModelTicketCloserScheduleOverlay,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_scope_overlay import (
    PACKAGED_OVERLAY,
    load_scope_overlay,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.bound_profile import (
    bound_closer_parts,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.gateway_open_prs import (
    GatewayOpenPullRequests,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.handler_ticket_closer_scheduled import (
    HandlerTicketCloserScheduled,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
SHA = "a" * 40
FIXTURE = (
    Path(__file__).parent
    / "fixtures/overlays/node_ticket_closer_scheduled_effect/overlay.yaml"
)


def fact(
    number: int, title: str, head_ref: str, *, draft: bool = False
) -> ModelOpenPrFact:
    return ModelOpenPrFact(
        number=number,
        head_ref=head_ref,
        head_sha=SHA,
        base_ref="dev",
        title=title,
        draft=draft,
    )


class FakeGateway:
    """A gateway client double: answers each repo from a table, records each request."""

    def __init__(
        self,
        by_repo: dict[str, list[ModelOpenPrFact]] | None = None,
        *,
        truncated: frozenset[str] = frozenset(),
        failing: frozenset[str] = frozenset(),
        wrong_type: frozenset[str] = frozenset(),
    ) -> None:
        self._by_repo = by_repo or {}
        self._truncated = truncated
        self._failing = failing
        self._wrong_type = wrong_type
        self.requests: list[ModelGithubGatewayRequest] = []

    def read(self, request: ModelGithubGatewayRequest) -> GithubGatewayResult:
        self.requests.append(request)
        if request.repo in self._failing:
            raise GatewayReadError(
                f"gateway open_pr_facts: no answer for {request.repo}"
            )
        if request.repo in self._wrong_type:
            return ModelOpenPrsResult(repo=request.repo, count=0, prs=[])
        prs = self._by_repo.get(request.repo, [])
        return ModelOpenPrFactsResult(
            repo=request.repo,
            prs=prs,
            total_count=len(prs),
            truncated=request.repo in self._truncated,
        )


def scope() -> ModelCloseoutScopeOverlay:
    return load_scope_overlay(PACKAGED_OVERLAY)


def source(gateway: FakeGateway) -> GatewayOpenPullRequests:
    return GatewayOpenPullRequests(gateway, scope())


def slug(name: str) -> str:
    return f"{scope().github_owner}/{name}"


def test_golden_the_open_set_comes_from_the_gateway_answer() -> None:
    gateway = FakeGateway(
        {
            slug("omnimarket"): [
                fact(5, "OMN-1: let the closer read the gateway (#5)", "omn-1-reads"),
                fact(6, "unrelated work", "omn2-other-work"),
            ]
        }
    )
    found = source(gateway).open_for(frozenset({"OMN-1", "OMN-2", "OMN-3"}))
    assert found == frozenset({"OMN-1", "OMN-2"})


def test_golden_one_open_pr_facts_read_per_scope_repository() -> None:
    gateway = FakeGateway()
    assert source(gateway).open_for(frozenset({"OMN-1"})) == frozenset()
    names = [repository.name for repository in scope().pr_repositories]
    assert [r.repo for r in gateway.requests] == [slug(name) for name in names]
    assert {r.operation for r in gateway.requests} == {
        EnumGithubGatewayOperation.OPEN_PR_FACTS
    }


def test_a_ticket_id_is_matched_as_the_watcher_state_matched_it() -> None:
    """The same title and head-ref fixtures as the watcher-state reading: title, loose branch id, evidence."""
    gateway = FakeGateway(
        {
            slug("omnimarket"): [
                fact(5, "OMN-1: let the closer read the watcher (#5)", "branch-a"),
                fact(7, "no id in the title", "OMN7-no-hyphen"),
                fact(8, "evidence(OMN-9): bindings", "omn-9-evidence"),
            ],
            slug("omnibase_core"): [fact(9, "title only OMN-4", "feature/x")],
        }
    )
    found = source(gateway).open_for(frozenset({"OMN-1", "OMN-4", "OMN-7", "OMN-9"}))
    assert found == frozenset({"OMN-1", "OMN-4", "OMN-7"})


def test_error_chain_a_failed_read_is_unread_and_names_why(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gateway = FakeGateway(failing=frozenset({slug("omnibase_infra")}))
    with caplog.at_level(logging.WARNING):
        found = source(gateway).open_for(frozenset({"OMN-1"}))
    assert found is None
    assert "no answer for" in caplog.text
    assert slug("omnibase_infra") in caplog.text


def test_error_chain_a_cut_answer_is_unread() -> None:
    gateway = FakeGateway(
        {slug("omnimarket"): [fact(5, "OMN-1: x (#5)", "omn-1-x")]},
        truncated=frozenset({slug("omnimarket")}),
    )
    assert source(gateway).open_for(frozenset({"OMN-1", "OMN-2"})) is None


def test_error_chain_an_answer_of_another_result_type_is_unread() -> None:
    gateway = FakeGateway(wrong_type=frozenset({slug("omnimarket")}))
    assert source(gateway).open_for(frozenset({"OMN-1"})) is None


def test_failing_control_an_unreadable_gateway_is_not_an_empty_open_set() -> None:
    readable = source(FakeGateway()).open_for(frozenset({"OMN-1"}))
    unreadable = source(FakeGateway(failing=frozenset({slug("omnimarket")}))).open_for(
        frozenset({"OMN-1"})
    )
    assert readable == frozenset()
    assert unreadable is None
    assert unreadable != readable


def test_a_failed_read_stops_the_remaining_reads() -> None:
    first = scope().pr_repositories[0].name
    gateway = FakeGateway(failing=frozenset({slug(first)}))
    assert source(gateway).open_for(frozenset({"OMN-1"})) is None
    assert len(gateway.requests) == 1


def contract_schedule() -> ModelTicketCloserScheduleOverlay:
    base = ModelTicketCloserScheduleOverlay.model_validate(
        yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    )
    return base.model_copy(
        update={
            "reading_source": EnumCloserReadingSource.CONTRACT_CLONES,
            "home_env": "TEST_HOME",
            "bus_lane": "dev",
            "ticket_timeout_seconds": 5.0,
            "scan_limit": 3,
        }
    )


def test_seam_a_closer_with_no_watcher_state_file_builds_its_parts_on_the_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The h201 case: clones mounted, no ``.claude_scratch/pr-watcher/state.json`` anywhere."""
    (tmp_path / scope().pr_repositories[0].name / ".git").mkdir(parents=True)
    assert not (tmp_path / ".claude_scratch").exists()
    monkeypatch.setattr(
        "omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.bound_profile.load_scope_overlay",
        lambda: scope(),
    )
    parts = bound_closer_parts(contract_schedule(), {"TEST_HOME": str(tmp_path)})
    candidate_source = parts.source
    assert vars(candidate_source)["_open_prs"].__class__ is GatewayOpenPullRequests


@pytest.mark.parametrize("gateway_up", [True, False])
async def test_golden_chain_the_closer_reads_candidates_through_the_gateway_double(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gateway_up: bool,
) -> None:
    """No watcher state file; a gateway double answers; the run reads (or says it could not)."""
    from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

    from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers import (
        bound_profile,
    )

    (tmp_path / scope().pr_repositories[0].name / ".git").mkdir(parents=True)
    monkeypatch.setattr(bound_profile, "load_scope_overlay", lambda: scope())
    gateway = FakeGateway(
        failing=frozenset() if gateway_up else frozenset({slug("omnimarket")})
    )
    real = bound_profile.GatewayOpenPullRequests
    monkeypatch.setattr(
        bound_profile,
        "GatewayOpenPullRequests",
        lambda *_: real(gateway, scope()),
    )
    monkeypatch.setattr(
        bound_profile.GitCloneContractIndex,
        "contract_ids",
        lambda *_: ["OMN-1"],
    )
    monkeypatch.setattr(
        bound_profile.GitCloneMergeReader,
        "merge",
        lambda *_: (str(tmp_path), SHA),
    )
    handler = HandlerTicketCloserScheduled(
        schedule=contract_schedule(), environ={"TEST_HOME": str(tmp_path)}
    )
    from uuid import uuid4

    result = await handler.handle(
        ModelRuntimeTick(
            now=NOW,
            tick_id=uuid4(),
            sequence_number=1,
            scheduled_at=NOW,
            correlation_id=uuid4(),
            scheduler_id="runtime-fixture",
            tick_interval_ms=1000,
        )
    )
    assert result is not None
    assert result.candidates_read is gateway_up
    if gateway_up:
        assert "unread=none" in result.status_line
    else:
        assert result.unread_reason.startswith(
            "the candidate source could not read its contracts, merges or pull requests"
        )
