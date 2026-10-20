# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The compaction pack build over the context-pack orchestrator (OMN-17427, TA.11 calls TA.8).

``HandlerCompactionPackBuild`` is the live ``ProtocolContextCompactionPackBuild``: it asks
the context-pack orchestrator for the ``compaction_orchestrator`` profile at the command's
session barrier and turns the built package, or its failure, into the build outcome the
compaction orchestrator installs or falls back on.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar
from uuid import UUID

import pytest
from omnibase_core.enums.enum_client_verdict_action import EnumClientVerdictAction
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_failure import EnumContextPackFailure
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from omnibase_core.models.context.model_context_access import ModelContextAccess
from omnibase_core.models.context.model_context_source_entry import (
    ModelContextSourceEntry,
)
from omnibase_core.models.context.model_context_source_request import (
    ModelContextSourceRequest,
)
from omnibase_core.models.context.model_context_source_result import (
    ModelContextSourceResult,
)

from omnimarket.models.context_compaction import (
    EnumContextCompactionFailureClass,
    EnumContextCompactionTrigger,
    ModelContextCompactionRequested,
)
from omnimarket.models.context_pack_orchestrator import (
    EnumContextPackOrchestratorStatus,
    ModelContextPackBuilt,
    ModelContextPackCommand,
)
from omnimarket.models.context_pack_render.model_context_pack_render_message import (
    ModelContextPackRenderMessage,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers.handler_compaction_pack_build import (
    COMPACTION_PROFILE,
    HandlerCompactionPackBuild,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers.handler_context_compaction_orchestrator import (
    HandlerContextCompactionOrchestrator,
)
from omnimarket.nodes.node_context_pack_orchestrator.handlers.handler_context_pack_orchestrator import (
    HandlerContextPackOrchestrator,
)
from omnimarket.nodes.node_context_profile_resolver_effect.handlers.handler_context_profile_resolver import (
    HandlerContextProfileResolver,
)

# The shipped compaction_orchestrator profile names extraction_proposer and
# context_compaction; the fixture promotes both in a temporary routing contract.
pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures("promoted_context_profile_routing"),
]

PROFILES_DIR = (
    Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "pack" / "profiles"
)
AS_OF = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
FIXED_IDENTITY = {"omnimarket.version": "0.0.0", "code.fixture": "a" * 64}
INVOCATION = UUID("11111111-1111-4111-8111-111111111111")
OTHER_INVOCATION = UUID("44444444-4444-4444-8444-444444444444")
TEXTS: dict[EnumContextFactor, str] = {
    EnumContextFactor.OPERATOR_RULINGS: "2026-10-02T13:16:36Z | RULING | lane=o | general",
    EnumContextFactor.OPERATOR_WORDS_UNRECORDED: "are compact work done",
    EnumContextFactor.ORCHESTRATION_STATE: "lanes: fill-m4 running",
    EnumContextFactor.OPEN_WORK: "2026-10-19T21:27:00Z | CLAIM | lane=fill-m4",
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _Source:
    """A context source answering one fixed entry and recording every request."""

    synchronous_execution: ClassVar[bool] = True

    def __init__(
        self,
        factor: EnumContextFactor,
        status: EnumContextSourceStatus = EnumContextSourceStatus.RESOLVED,
    ) -> None:
        self.factor = factor
        self.status = status
        self.requests: list[ModelContextSourceRequest] = []

    def handle(self, request: ModelContextSourceRequest) -> ModelContextSourceResult:
        self.requests.append(request)
        if self.status is not EnumContextSourceStatus.RESOLVED:
            return ModelContextSourceResult(status=self.status)
        text = TEXTS[self.factor]
        return ModelContextSourceResult(
            status=self.status,
            entries=(
                ModelContextSourceEntry(
                    factor=self.factor,
                    text=text,
                    source_ref=f"fixture/{self.factor.value}@0000000",
                    text_sha256=_sha(text),
                    status=EnumContextSourceStatus.RESOLVED,
                    as_of=AS_OF,
                ),
            ),
        )


def _sources(**statuses: EnumContextSourceStatus) -> dict[EnumContextFactor, _Source]:
    return {
        factor: _Source(
            factor, statuses.get(factor.value, EnumContextSourceStatus.RESOLVED)
        )
        for factor in TEXTS
    }


def _pack_orchestrator(
    sources: dict[EnumContextFactor, _Source],
) -> HandlerContextPackOrchestrator:
    return HandlerContextPackOrchestrator(
        sources,
        profile_resolver=HandlerContextProfileResolver(PROFILES_DIR),
        implementation_identity=lambda: dict(FIXED_IDENTITY),
    )


def _access() -> ModelContextAccess:
    return ModelContextAccess(tenant_id="tenant-a")


def _request(
    invocation_id: UUID = INVOCATION, barrier_id: str | None = "barrier-7"
) -> ModelContextCompactionRequested:
    return ModelContextCompactionRequested(
        invocation_id=invocation_id,
        session_id="session-1",
        cwd="/work/repo",
        trigger=EnumContextCompactionTrigger.MANUAL,
        input_messages_hash=_sha("input"),
        newest_turn="turn-9",
        barrier_id=barrier_id,
    )


class _CannedPackOrchestrator:
    """Answers one fixed built result and records the command it was given."""

    def __init__(self, built: ModelContextPackBuilt) -> None:
        self.built = built
        self.commands: list[ModelContextPackCommand] = []

    def handle(self, command: ModelContextPackCommand) -> ModelContextPackBuilt:
        self.commands.append(command)
        return self.built


def _ok_built(**fields: object) -> ModelContextPackBuilt:
    payload: dict[str, object] = {
        "status": EnumContextPackOrchestratorStatus.OK,
        "profile_name": COMPACTION_PROFILE,
        "profile_content_hash": _sha("profile"),
        "candidate_id": "22222222-2222-5222-8222-222222222222",
        "assembly_hash": _sha("assembly"),
        "final_pack_hash": _sha("final"),
        "body_hash": _sha("body"),
        "version_vector_hash": _sha("vector"),
        "messages": (ModelContextPackRenderMessage(role="user", text="pack"),),
    }
    payload.update(fields)
    return ModelContextPackBuilt.model_validate(payload)


def test_profile_is_compaction_orchestrator() -> None:
    assert COMPACTION_PROFILE == "compaction_orchestrator"


def test_command_names_profile_session_barrier_and_access() -> None:
    canned = _CannedPackOrchestrator(_ok_built())
    HandlerCompactionPackBuild(canned, access=_access()).build(_request())
    (command,) = canned.commands
    assert command.request.profile_name == COMPACTION_PROFILE
    assert command.request.session_id == "session-1"
    assert command.request.newest_turn == "turn-9"
    assert command.request.session_barrier == "barrier-7"
    assert command.access == _access()


def test_built_package_carries_the_pack_identities() -> None:
    built = _pack_orchestrator(_sources()).handle(
        ModelContextPackCommand.model_validate(
            {
                "request": {
                    "profile_name": COMPACTION_PROFILE,
                    "session_id": "session-1",
                    "newest_turn": "turn-9",
                    "session_barrier": "barrier-7",
                },
                "access": _access(),
            }
        )
    )
    assert built.status is EnumContextPackOrchestratorStatus.OK
    outcome = HandlerCompactionPackBuild(
        _pack_orchestrator(_sources()), access=_access()
    ).build(_request())
    package = outcome.package
    assert package is not None
    assert outcome.failure_class is None
    assert package.candidate_id == UUID(str(built.candidate_id))
    assert package.assembly_hash == built.assembly_hash
    assert package.final_pack_hash == built.final_pack_hash
    assert package.body_hash == built.body_hash
    assert package.version_vector_hash == built.version_vector_hash
    assert package.profile_name == COMPACTION_PROFILE
    assert package.profile_content_hash == built.profile_content_hash
    assert package.messages == built.messages


def test_model_outputs_hash_is_over_the_canonical_model_outputs() -> None:
    outputs = ("summary one", "summary two")
    outcome = HandlerCompactionPackBuild(
        _CannedPackOrchestrator(_ok_built(model_outputs=outputs)), access=_access()
    ).build(_request())
    assert outcome.package is not None
    expected = _sha(json.dumps(list(outputs), separators=(",", ":")))
    assert outcome.package.model_outputs_hash == expected


def test_sources_read_at_the_barrier_for_the_access_tenant() -> None:
    sources = _sources()
    HandlerCompactionPackBuild(_pack_orchestrator(sources), access=_access()).build(
        _request()
    )
    for source in sources.values():
        (read,) = source.requests
        assert read.revision == "barrier-7"
        assert read.access.tenant_id == "tenant-a"


def test_required_tier_missing_is_a_pack_build_failure() -> None:
    sources = _sources(open_work=EnumContextSourceStatus.UNAVAILABLE)
    outcome = HandlerCompactionPackBuild(
        _pack_orchestrator(sources), access=_access()
    ).build(_request())
    assert outcome.package is None
    assert outcome.failure_class is EnumContextCompactionFailureClass.PACK_BUILD_FAILED


@pytest.mark.parametrize(
    "missing",
    [
        "candidate_id",
        "assembly_hash",
        "final_pack_hash",
        "body_hash",
        "version_vector_hash",
        "profile_content_hash",
    ],
)
def test_ok_build_missing_an_identity_is_a_pack_build_failure(missing: str) -> None:
    outcome = HandlerCompactionPackBuild(
        _CannedPackOrchestrator(_ok_built(**{missing: None})), access=_access()
    ).build(_request())
    assert outcome.package is None
    assert outcome.failure_class is EnumContextCompactionFailureClass.PACK_BUILD_FAILED


def test_ok_build_with_no_messages_is_a_pack_build_failure() -> None:
    outcome = HandlerCompactionPackBuild(
        _CannedPackOrchestrator(_ok_built(messages=())), access=_access()
    ).build(_request())
    assert outcome.failure_class is EnumContextCompactionFailureClass.PACK_BUILD_FAILED


def test_failed_build_is_a_pack_build_failure() -> None:
    failed = ModelContextPackBuilt(
        status=EnumContextPackOrchestratorStatus.FAILED,
        profile_name=COMPACTION_PROFILE,
        failure_class=EnumContextPackFailure.PROFILE_NOT_FOUND,
    )
    outcome = HandlerCompactionPackBuild(
        _CannedPackOrchestrator(failed), access=_access()
    ).build(_request())
    assert outcome.failure_class is EnumContextCompactionFailureClass.PACK_BUILD_FAILED


def test_compaction_installs_the_pack_built_through_the_adapter() -> None:
    handler = HandlerContextCompactionOrchestrator(
        pack_build=HandlerCompactionPackBuild(
            _pack_orchestrator(_sources()), access=_access()
        )
    )
    result = handler.handle(_request())
    assert result.reply.action is EnumClientVerdictAction.INSTALL
    assert result.fallback is None
    assert result.prepared is not None
    assert result.prepared.profile_name == COMPACTION_PROFILE
    assert result.activated is not None
    assert result.reply.messages[-1].text.endswith(
        f"activation_id: {result.activated.activation_id}"
    )


def test_compaction_falls_back_native_when_a_required_tier_is_missing() -> None:
    handler = HandlerContextCompactionOrchestrator(
        pack_build=HandlerCompactionPackBuild(
            _pack_orchestrator(
                _sources(operator_rulings=EnumContextSourceStatus.UNAVAILABLE)
            ),
            access=_access(),
        )
    )
    result = handler.handle(_request())
    assert result.reply.action is EnumClientVerdictAction.NATIVE
    assert result.reply.messages == ()
    assert result.fallback is not None
    assert (
        result.fallback.failure_class
        is EnumContextCompactionFailureClass.PACK_BUILD_FAILED
    )
    assert result.fallback.invocation_id == INVOCATION


def test_two_invocations_on_the_same_inputs_share_one_candidate() -> None:
    """TA.11: same candidate_id and assembly_hash, different activation_ids."""
    handler = HandlerContextCompactionOrchestrator(
        pack_build=HandlerCompactionPackBuild(
            _pack_orchestrator(_sources()), access=_access()
        )
    )
    first = handler.handle(_request(INVOCATION))
    second = handler.handle(_request(OTHER_INVOCATION))
    assert first.prepared is not None
    assert second.prepared is not None
    assert first.activated is not None
    assert second.activated is not None
    assert first.prepared.candidate_id == second.prepared.candidate_id
    assert first.prepared.assembly_hash == second.prepared.assembly_hash
    assert first.activated.activation_id != second.activated.activation_id
