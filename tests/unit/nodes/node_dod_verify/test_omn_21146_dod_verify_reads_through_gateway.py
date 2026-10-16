# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-21146 -- node_dod_verify reads PR facts and check runs through the GitHub
gateway node, never the ``gh`` CLI.

A ticket whose PRs merged outside the PR watcher's window has only their squash
commits on the canonical clone: no head, no merge time. The verify used to ask
``gh pr view`` for those, and the repo-evidence check runs from ``gh api``; on a
lab lane the ``gh`` shim refuses both, so the verdict was
REPO_CONTRACT_UNRESOLVED. Here a ``gh`` on PATH fails on any call and records
it, and node_github_repo_gateway_effect answers from the world.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.enums.enum_dod_verify_execution_audience import (
    EnumDodVerifyExecutionAudience,
)
from omnimarket.handlers.scm import handler_gateway_read_client as client_mod
from omnimarket.handlers.scm.handler_gateway_read_client import (
    HandlerGatewayReadClient,
)
from omnimarket.nodes.node_dod_verify.handlers import (
    handler_dod_evidence_github_effect as hd_mod,
)
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_evidence_github_effect import (
    HandlerDurableEvidenceGateEffect,
)
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    ModelDodVerifyState,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)
from omnimarket.nodes.node_github_repo_gateway_effect.models.model_gateway_io import (
    EnumGithubGatewayOperation,
    ModelGithubGatewayRequest,
    ModelMergeCommitShaResult,
)
from tests.unit.nodes.node_dod_verify.omn_20886_repo_world import World, contract
from tests.unit.nodes.node_dod_verify.omn_21146_world_gateway import WorldGateway

pytestmark = pytest.mark.unit

_REAL_RUN_GATE = EvidenceCollector.run_durable_gate
_AUDIENCE = EnumDodVerifyExecutionAudience.LOCAL_DONE_GATE
_DESCRIPTION = (
    "## Acceptance criteria\n\n- [ ] AC1: it works -- falsifier: uv run pytest t.py\n"
)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _verify(
    world: World, monkeypatch: pytest.MonkeyPatch, ticket: str
) -> tuple[ModelDodVerifyState, WorldGateway]:
    world.install(monkeypatch)
    # A commit without the gateway client has no seam; the gh stub still
    # records what that commit asks gh for.
    seam = getattr(hd_mod, "_default_gateway_client", None)
    gateway = seam() if seam is not None else WorldGateway({}, {})
    assert isinstance(gateway, WorldGateway)
    monkeypatch.setattr(EvidenceCollector, "run_durable_gate", _REAL_RUN_GATE)
    monkeypatch.setattr(
        HandlerDurableEvidenceGateEffect,
        "read_ticket",
        staticmethod(lambda _ticket: (_DESCRIPTION, frozenset(), "")),
    )
    collector = EvidenceCollector()
    monkeypatch.setattr(
        HandlerDodVerify, "_make_collector", staticmethod(lambda: collector)
    )
    state = HandlerDodVerify().handle(
        ModelDodVerifyStartCommand(ticket_id=ticket, execution_audience=_AUDIENCE)
    )
    assert isinstance(state, ModelDodVerifyState)
    return state, gateway


def _messages(state: ModelDodVerifyState) -> list[str]:
    return [c.message or "" for c in state.checks]


def test_prs_outside_the_watcher_window_resolve_through_the_gateway_with_no_gh(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: the failing gh stub is never called, and the verdict is not
    REPO_CONTRACT_UNRESOLVED."""
    ticket = "OMN-90301"
    world.merge(
        "omnimarket",
        41,
        f"feat({ticket}): first",
        {f"contracts/{ticket}.yaml": contract(ticket, "dod-bound", ["AC1"])},
    )
    newest = world.merge(
        "omnimarket",
        42,
        f"fix({ticket}): second",
        {"src/x.py": "x = 1\n"},
    )
    world.forget("omnimarket", 41)
    world.forget("omnimarket", 42)

    state, gateway = _verify(world, monkeypatch, ticket)

    assert world.gh_calls() == []
    assert not any(m.startswith("REPO_CONTRACT_UNRESOLVED") for m in _messages(state))
    assert "error_github_api" not in " ".join(_messages(state))
    operations = {(r.operation, r.pr_number, r.head_sha) for r in gateway.requests}
    assert (EnumGithubGatewayOperation.MERGED_PR, 41, None) in operations
    assert (EnumGithubGatewayOperation.MERGED_PR, 42, None) in operations
    assert (
        EnumGithubGatewayOperation.COMMIT_CHECK_RUNS,
        None,
        newest["head_sha"],
    ) in operations


def test_a_pr_the_gateway_cannot_read_refuses_and_still_calls_no_gh(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed: an unreadable PR is REPO_CONTRACT_UNRESOLVED naming the
    gateway, and gh is not asked in its place."""
    ticket = "OMN-90302"
    world.merge(
        "omnimarket",
        51,
        f"feat({ticket}): only",
        {f"contracts/{ticket}.yaml": contract(ticket, "dod-bound", ["AC1"])},
    )
    world.forget("omnimarket", 51)
    del world.github_prs["omnimarket#51"]

    state, _gateway = _verify(world, monkeypatch, ticket)

    assert world.gh_calls() == []
    messages = _messages(state)
    assert any(
        m.startswith("REPO_CONTRACT_UNRESOLVED") and "gateway merged_pr" in m
        for m in messages
    ), messages


def test_unreadable_check_runs_refuse_through_the_gateway(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = "OMN-90303"
    pr = world.merge(
        "omnimarket",
        61,
        f"feat({ticket}): only",
        {f"contracts/{ticket}.yaml": contract(ticket, "dod-bound", ["AC1"])},
    )
    world.check_runs[("OmniNode-ai/omnimarket", pr["head_sha"])] = [
        {"unreadable": True}
    ]

    state, _gateway = _verify(world, monkeypatch, ticket)

    assert world.gh_calls() == []
    assert any(m.startswith("REPO_CONTRACT_UNRESOLVED") for m in _messages(state))


def test_client_reads_in_process_when_the_gateway_token_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = ModelMergeCommitShaResult(
        repo="OmniNode-ai/omnimarket", pr_number=7, merged=True
    )
    monkeypatch.setattr(client_mod, "_token_resolves", lambda: True)
    monkeypatch.setattr(client_mod, "read_in_process", lambda _r: answer)
    monkeypatch.setattr(
        client_mod,
        "read_over_bus",
        lambda _r: pytest.fail("bus read with a resolvable token"),
    )
    request = ModelGithubGatewayRequest(
        operation=EnumGithubGatewayOperation.MERGE_COMMIT_SHA,
        repo="OmniNode-ai/omnimarket",
        pr_number=7,
    )
    assert HandlerGatewayReadClient().read(request) is answer


def test_client_asks_the_deployed_gateway_over_the_bus_without_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = ModelMergeCommitShaResult(
        repo="OmniNode-ai/omnimarket", pr_number=7, merged=True
    )
    monkeypatch.setattr(client_mod, "_token_resolves", lambda: False)
    monkeypatch.setattr(
        client_mod,
        "read_in_process",
        lambda _r: pytest.fail("in-process read without a token"),
    )
    monkeypatch.setattr(client_mod, "read_over_bus", lambda _r: answer)
    request = ModelGithubGatewayRequest(
        operation=EnumGithubGatewayOperation.MERGE_COMMIT_SHA,
        repo="OmniNode-ai/omnimarket",
        pr_number=7,
    )
    assert HandlerGatewayReadClient().read(request) is answer
