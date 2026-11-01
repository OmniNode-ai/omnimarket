# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_pr_handoff_orchestrator: the landing scope is loaded at composition and passed to the decision.

The scope is the ``landing_scope`` overlay (``<root>/landing_scope/overlay.yaml`` under a
root named in ``ONEX_SKILL_OVERLAY_ROOTS``). Repository names in the fixtures are invented.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.pr_handoff import (
    EnumPrHandoffErrorCode,
    ModelPrHandoffDecision,
    ModelPrHandoffDecisionRequest,
    ModelPrHandoffFailed,
    ModelPrHandoffLandingScopeReading,
    ModelPrHandoffLedgerAppendCommand,
)
from omnimarket.nodes.node_binding_conformance_compute.handlers.handler_binding_conformance import (
    HandlerBindingConformance,
)
from omnimarket.nodes.node_binding_parity_orchestrator.handlers.handler_run_local import (
    run_local,
)
from omnimarket.nodes.node_binding_parity_orchestrator.models.model_run_local import (
    ModelRunLocalConfig,
)
from omnimarket.nodes.node_pr_handoff_decision_compute.handlers.handler_pr_handoff_decision import (
    HandlerPrHandoffDecision,
)
from omnimarket.nodes.node_pr_handoff_orchestrator.handlers import (
    HandlerPrHandoffOrchestrator,
)
from omnimarket.nodes.node_pr_handoff_orchestrator.landing_scope import (
    LANDING_SCOPE_OVERLAY,
    read_landing_scope,
)
from omnimarket.nodes.node_pr_handoff_orchestrator.orchestration.row_store import (
    InMemoryPrHandoffRowStore,
)
from tests.chains.pr_handoff import _builders as b
from tests.nodes.node_binding_conformance_compute.test_check_bindings import (
    DATA,
    _real_request,
)

pytestmark = pytest.mark.unit

E = EnumPrHandoffErrorCode
ROOTS = "ONEX_SKILL_OVERLAY_ROOTS"
# Two deployments' ``landing_scope`` overlays: the same request is refused under
# one and ready under the other.
LANDING_THE_REPO = {"repos": [b.REPO, "acme-gadgets"]}
NOT_LANDING_THE_REPO = {"repos": ["acme-gadgets"]}


def _overlay(root: Path, document: object) -> dict[str, str]:
    directory = root / LANDING_SCOPE_OVERLAY
    directory.mkdir(parents=True)
    text = document if isinstance(document, str) else yaml.safe_dump(document)
    (directory / "overlay.yaml").write_text(text, encoding="utf-8")
    return {ROOTS: str(root)}


async def _handoff(handler: HandlerPrHandoffOrchestrator) -> list[Any]:
    await handler.handle(b.observation(b.at(-10)))
    return list(await handler.handle(b.request(b.new_cid())))


def _handler(environ: dict[str, str]) -> HandlerPrHandoffOrchestrator:
    return HandlerPrHandoffOrchestrator(
        store=InMemoryPrHandoffRowStore(), environ=environ
    )


def _failed(emitted: list[Any]) -> ModelPrHandoffFailed:
    failed = emitted[-1]
    assert isinstance(failed, ModelPrHandoffFailed)
    return failed


async def test_an_out_of_scope_handoff_is_refused_through_the_orchestrator(
    tmp_path: Path,
) -> None:
    environ = _overlay(tmp_path, NOT_LANDING_THE_REPO)
    failed = _failed(await _handoff(_handler(environ)))
    assert failed.error_code is E.REPO_OUT_OF_LANDING_SCOPE


async def test_the_same_request_is_ready_under_the_other_overlay(
    tmp_path: Path,
) -> None:
    environ = _overlay(tmp_path, LANDING_THE_REPO)
    emitted = await _handoff(_handler(environ))
    assert isinstance(emitted[-1], ModelPrHandoffLedgerAppendCommand)


async def test_with_the_scope_load_removed_the_handoff_is_refused_scope_unread() -> (
    None
):
    removed = HandlerPrHandoffOrchestrator(
        store=InMemoryPrHandoffRowStore(),
        landing_scope=ModelPrHandoffLandingScopeReading(),
    )
    assert _failed(await _handoff(removed)).error_code is E.SCOPE_UNREAD


async def test_no_overlay_configured_is_refused_scope_unread_naming_the_key() -> None:
    failed = _failed(await _handoff(_handler({})))
    assert failed.error_code is E.SCOPE_UNREAD
    assert LANDING_SCOPE_OVERLAY in failed.detail


@pytest.mark.parametrize(
    "document",
    ["repos: [unterminated", "- not\n- a mapping\n", {"repos": [b.REPO, ""]}, {}],
    ids=["unparseable", "not-a-mapping", "blank-repo", "no-repos"],
)
async def test_an_unreadable_or_invalid_overlay_is_refused_scope_unread(
    tmp_path: Path, document: object
) -> None:
    environ = _overlay(tmp_path, document)
    reading = read_landing_scope(environ)
    assert reading.repos is None
    assert reading.problem is not None
    assert b.REPO not in reading.problem
    assert _failed(await _handoff(_handler(environ))).error_code is E.SCOPE_UNREAD


def test_the_overlay_is_read_into_the_scope(tmp_path: Path) -> None:
    reading = read_landing_scope(_overlay(tmp_path, LANDING_THE_REPO))
    assert reading == ModelPrHandoffLandingScopeReading(repos=(b.REPO, "acme-gadgets"))


def _decision_request(scope: tuple[str, ...]) -> ModelPrHandoffDecisionRequest:
    return ModelPrHandoffDecisionRequest(
        request=b.request(b.new_cid()),
        observation=b.observation(b.at(60)).observation(),
        landing_scope=scope,
        now=b.at(60),
    )


def test_the_decision_is_byte_identical_in_process_and_through_the_local_entry() -> (
    None
):
    config = ModelRunLocalConfig(
        command_topic="onex.cmd.omnimarket.pr-handoff-decision-requested.v1",
        result_topic="onex.evt.omnimarket.pr-handoff-decision-completed.v1",
        consumer_group="pr-handoff-decision-scope",
        timeout_seconds=30,
    )
    for scope in ((b.REPO,), ("acme-gadgets",), ()):
        request = _decision_request(scope)
        direct = HandlerPrHandoffDecision().handle(request)
        payload = json.loads(request.model_dump_json())
        local = asyncio.run(
            run_local(
                "node_pr_handoff_decision_compute", "decide_pr_handoff", payload, config
            )
        )
        assert (
            ModelPrHandoffDecision.model_validate(local).model_dump_json()
            == direct.model_dump_json()
        )
    unread = HandlerPrHandoffDecision().handle(
        ModelPrHandoffDecisionRequest(
            request=b.request(b.new_cid()),
            observation=b.observation(b.at(60)).observation(),
            now=b.at(60),
        )
    )
    assert unread.error_code is E.SCOPE_UNREAD


def test_the_binding_check_passes_over_the_orchestrator_contract() -> None:
    assert "node_pr_handoff_orchestrator" in DATA["checked_nodes"]
    result = HandlerBindingConformance().handle(
        _real_request(["node_pr_handoff_orchestrator"])
    )
    assert result.issues == ()
    assert result.conforming is True
