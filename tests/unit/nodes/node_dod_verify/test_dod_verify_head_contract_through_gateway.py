# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""node_dod_verify reads a product repository's contract at a PR's HEAD commit
through the GitHub gateway node, never the ``gh`` CLI.

A squash-merged PR's own head commit is not held by the canonical clone (the
clone holds only the squash commit), so the contract at that head must be read
through the gateway's ``repo_file`` operation. On a lab lane the ``gh`` shim
refuses that raw read, so the verify used to stop on
durable_gate::contract_on_occ_main. Here a ``gh`` on PATH fails on any call and
records it, and node_github_repo_gateway_effect answers the file read from the
world.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.enums.enum_dod_verify_execution_audience import (
    EnumDodVerifyExecutionAudience,
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
)
from tests.unit.nodes.node_dod_verify.omn_20886_repo_world import World, contract
from tests.unit.nodes.node_dod_verify.omn_21146_world_gateway import WorldGateway

pytestmark = pytest.mark.unit

_REAL_RUN_GATE = EvidenceCollector.run_durable_gate
_AUDIENCE = EnumDodVerifyExecutionAudience.LOCAL_DONE_GATE
_DESCRIPTION = (
    "## Acceptance criteria\n\n- [ ] AC1: it works -- falsifier: uv run pytest t.py\n"
)
_REPO = "OmniNode-ai/omnimarket"
_HEAD = "c" * 40


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _verify(
    world: World, monkeypatch: pytest.MonkeyPatch, ticket: str
) -> tuple[ModelDodVerifyState, WorldGateway]:
    world.install(monkeypatch)
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


def _head_world(world: World, ticket: str, contract_text: str) -> None:
    """Model a squash-merged PR whose head commit the clone does NOT hold."""
    pr = world.merge(
        "omnimarket",
        101,
        f"feat({ticket}): head",
        {f"contracts/{ticket}.yaml": contract_text},
    )
    old_head = pr["head_sha"]
    world.state["merges"]["omnimarket#101"]["head_sha"] = _HEAD
    world.github_prs["omnimarket#101"]["head_sha"] = _HEAD
    world.check_runs[(_REPO, _HEAD)] = world.check_runs.pop((_REPO, old_head))
    world.repo_files[(_REPO, _HEAD, f"contracts/{ticket}.yaml")] = contract_text


def test_head_contract_is_read_through_the_gateway(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Golden: the verify reads the head contract through the gateway, gh is
    never called, and durable_gate::contract_on_occ_main passes."""
    ticket = "OMN-90401"
    text = contract(ticket, "dod-bound", ["AC1"])
    _head_world(world, ticket, text)

    state, gateway = _verify(world, monkeypatch, ticket)

    assert world.gh_calls() == []
    repo_file_requests = [
        r
        for r in gateway.requests
        if r.operation is EnumGithubGatewayOperation.REPO_FILE
    ]
    assert any(
        r.head_sha == _HEAD and r.file_path == f"contracts/{ticket}.yaml"
        for r in repo_file_requests
    ), gateway.requests
    assert not any(m.startswith("REPO_CONTRACT_UNRESOLVED") for m in _messages(state))
    assert not any(
        m.startswith("Product-repository contract refused") for m in _messages(state)
    ), _messages(state)
    assert any(
        m.startswith("Product-repository contract governs") for m in _messages(state)
    ), _messages(state)


def test_unreadable_head_contract_refuses_and_still_calls_no_gh(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error chain: the gateway answers UNREADABLE for the head file, so the
    verdict is a refusal naming ``gateway repo_file`` and gh is never called."""
    ticket = "OMN-90402"
    text = contract(ticket, "dod-bound", ["AC1"])
    _head_world(world, ticket, text)
    world.repo_files[(_REPO, _HEAD, f"contracts/{ticket}.yaml")] = "UNREADABLE"

    state, _gateway = _verify(world, monkeypatch, ticket)

    assert world.gh_calls() == []
    assert any(
        m.startswith("Product-repository contract refused")
        and f"contracts/{ticket}.yaml is error (gateway repo_file" in m
        for m in _messages(state)
    ), _messages(state)


def test_absent_head_contract_refuses_and_still_calls_no_gh(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error chain: the gateway holds no file at the head (found False), so the
    contract at the head is absent and the gate refuses."""
    ticket = "OMN-90403"
    text = contract(ticket, "dod-bound", ["AC1"])
    _head_world(world, ticket, text)
    del world.repo_files[(_REPO, _HEAD, f"contracts/{ticket}.yaml")]

    state, _gateway = _verify(world, monkeypatch, ticket)

    assert world.gh_calls() == []
    assert any(
        m.startswith("Product-repository contract refused")
        and f"contracts/{ticket}.yaml is absent" in m
        for m in _messages(state)
    ), _messages(state)
