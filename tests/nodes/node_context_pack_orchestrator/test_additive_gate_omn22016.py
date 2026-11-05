# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The server-side list of enabled additive profiles and the verdict an additive call gets (OMN-22016, TC.11)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from pydantic import ValidationError

from omnimarket.enums.enum_context_binding_kind import EnumContextBindingKind
from omnimarket.models.claude_code_binding import EnumClientVerdictAction
from omnimarket.models.context_pack_orchestrator import (
    EnumContextPackOrchestratorStatus,
    ModelContextPackBuilt,
    ModelContextPackCommand,
    ModelContextPackRequest,
)
from omnimarket.nodes.node_context_pack_orchestrator.container.container_context_pack_orchestrator import (
    create_handler_context_pack_orchestrator,
)
from omnimarket.nodes.node_context_pack_orchestrator.handlers import (
    HandlerContextPackOrchestratorRuntime,
    load_enabled_additive_profiles,
)

from .conftest import FixtureSource, access

pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures("promoted_context_profile_routing"),
]

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_context_pack_orchestrator/contract.yaml"
)
INVOCATION = UUID("11111111-1111-4111-8111-111111111111")
TEXTS = {
    EnumContextFactor.SESSION_GOAL: "goal",
    EnumContextFactor.OPERATOR_RULINGS: "2026-10-15T08:14:15Z | RULING | lane=x | r",
    EnumContextFactor.OPEN_WORK: "2026-10-17T22:36:14Z | CLAIM | lane=x | ticket=OMN-1",
}


def _command(profile: str, *, additive: bool = True) -> ModelContextPackCommand:
    return ModelContextPackCommand(
        request=ModelContextPackRequest(
            profile_name=profile,
            session_id="s-1",
            binding_kind=EnumContextBindingKind.ADDITIVE if additive else None,
            invocation_id=INVOCATION if additive else None,
        ),
        access=access(),
    )


def _sources() -> dict[EnumContextFactor, FixtureSource]:
    return {f: FixtureSource(f, text=t) for f, t in TEXTS.items()}


def test_additive_list_empty_by_default_and_in_the_shipped_contract() -> None:
    assert load_enabled_additive_profiles() == ()
    assert yaml.safe_load(CONTRACT.read_text())["enabled_additive_profiles"] == []


@pytest.mark.parametrize("profile", ["session_start", "agent_spawn", "per_prompt"])
def test_additive_list_empty_answers_none_and_reads_no_source(profile: str) -> None:
    sources = _sources()
    handler = HandlerContextPackOrchestratorRuntime(
        sources, enabled_additive_profiles=()
    )
    built = handler.handle(_command(profile))
    assert built.status is EnumContextPackOrchestratorStatus.NOT_ENABLED
    assert built.action is EnumClientVerdictAction.NONE
    assert built.invocation_id == INVOCATION
    assert (built.messages, built.text, built.package_sha256) == ((), None, None)
    assert all(not source.requests for source in sources.values())


def test_additive_list_empty_runtime_construction_reads_the_contract_list() -> None:
    built = HandlerContextPackOrchestratorRuntime().handle(_command("session_start"))
    assert built.status is EnumContextPackOrchestratorStatus.NOT_ENABLED


def test_an_enabled_additive_profile_is_answered_inject_with_the_hash_of_its_text() -> (
    None
):
    handler = HandlerContextPackOrchestratorRuntime(
        _sources(), enabled_additive_profiles=("session_start",)
    )
    built = handler.handle(_command("session_start"))
    assert built.status is EnumContextPackOrchestratorStatus.OK, built.errors
    assert built.action is EnumClientVerdictAction.INJECT
    assert built.invocation_id == INVOCATION
    assert built.text == "\n\n".join(m.text for m in built.messages)
    assert built.package_sha256 == hashlib.sha256(built.text.encode()).hexdigest()
    # The wire payload carries `action`, which is what ends the receipt-mode wait.
    assert built.model_dump(mode="json")["action"] == "inject"
    # The same profile called inline carries no verdict and does not need the list.
    inline = create_handler_context_pack_orchestrator(_sources()).handle(
        _command("session_start", additive=False)
    )
    assert inline.status is EnumContextPackOrchestratorStatus.OK, inline.errors
    assert (inline.action, inline.text, inline.package_sha256) == (None, None, None)
    assert inline.messages == built.messages


def test_a_failed_additive_build_is_answered_none_so_the_hook_injects_nothing() -> None:
    sources = _sources()
    sources[EnumContextFactor.OPEN_WORK] = FixtureSource(
        EnumContextFactor.OPEN_WORK, EnumContextSourceStatus.UNAVAILABLE
    )
    built = HandlerContextPackOrchestratorRuntime(
        sources, enabled_additive_profiles=("session_start",)
    ).handle(_command("session_start"))
    assert built.status is EnumContextPackOrchestratorStatus.FAILED
    assert built.action is EnumClientVerdictAction.NONE
    assert built.invocation_id == INVOCATION
    assert built.terminal_failure_cause is not None


def test_a_profile_not_on_the_list_stays_unbuilt_while_another_is_enabled() -> None:
    built = HandlerContextPackOrchestratorRuntime(
        _sources(), enabled_additive_profiles=("agent_spawn",)
    ).handle(_command("session_start"))
    assert built.status is EnumContextPackOrchestratorStatus.NOT_ENABLED


def test_an_additive_request_names_its_invocation_and_only_it_does() -> None:
    with pytest.raises(ValidationError, match="invocation_id"):
        ModelContextPackRequest(
            profile_name="session_start", binding_kind=EnumContextBindingKind.ADDITIVE
        )
    with pytest.raises(ValidationError, match="invocation_id"):
        ModelContextPackRequest(profile_name="session_start", invocation_id=INVOCATION)
    assert ModelContextPackBuilt.model_fields["action"].default is None


def _contract_with(tmp_path: Path, names: object) -> Path:
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump({"enabled_additive_profiles": names}))
    return path


def test_additive_list_loader_accepts_the_promotable_profiles(tmp_path: Path) -> None:
    path = _contract_with(tmp_path, ["session_start", "agent_spawn"])
    assert load_enabled_additive_profiles(path) == ("session_start", "agent_spawn")


def test_additive_list_loader_refuses_per_prompt_until_tc13(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="before its promotion"):
        load_enabled_additive_profiles(_contract_with(tmp_path, ["per_prompt"]))


@pytest.mark.parametrize(
    ("names", "message"),
    [
        (["compaction_orchestrator"], "not an additive profile"),
        (["session_start", "session_start"], "repeats"),
        ("session_start", "list of profile names"),
        ([1], "list of profile names"),
    ],
)
def test_additive_list_loader_refuses_a_bad_list(
    names: object, message: str, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match=message):
        load_enabled_additive_profiles(_contract_with(tmp_path, names))
