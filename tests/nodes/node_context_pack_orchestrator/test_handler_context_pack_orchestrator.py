# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerContextPackOrchestrator criteria (OMN-21110, plan task TA.8)."""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_failure import EnumContextPackFailure
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from omnibase_core.models.context.model_context_caller_scope import (
    ModelContextCallerScope,
)
from omnibase_core.models.context.model_context_source_request import (
    ModelContextSourceRequest,
)
from omnibase_core.models.context.model_context_source_result import (
    ModelContextSourceResult,
)
from pydantic import ValidationError

from omnimarket.models.context_pack_orchestrator import (
    EnumContextPackOrchestratorStatus,
    ModelContextPackRequest,
)
from omnimarket.models.context_profile_resolve.model_context_profile_resolve_request import (
    ModelContextProfileResolveRequest,
)
from omnimarket.nodes.node_context_pack_orchestrator.handlers.handler_context_pack_orchestrator import (
    TASK_KIND_UNKNOWN,
    default_implementation_identity,
)
from omnimarket.nodes.node_context_pack_orchestrator.protocols import (
    ProtocolContextTierSource,
)
from omnimarket.nodes.node_context_profile_resolver_effect.handlers.handler_context_profile_resolver import (
    HandlerContextProfileResolver,
)

from .conftest import (
    COMPACTION_REQUIRED,
    FixtureSource,
    command,
    orchestrator,
    sources,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures("promoted_context_profile_routing"),
]


def test_fixture_source_is_a_protocol_context_tier_source() -> None:
    assert isinstance(
        FixtureSource(EnumContextFactor.OPEN_WORK), ProtocolContextTierSource
    )


def test_compaction_fixture_builds_with_four_required_factors() -> None:
    built = orchestrator().handle(command())
    assert built.status is EnumContextPackOrchestratorStatus.OK, built.errors
    expected = HandlerContextProfileResolver().handle(
        ModelContextProfileResolveRequest(profile_name="compaction_orchestrator")
    )
    assert built.profile_name == "compaction_orchestrator"
    assert built.profile_content_hash == expected.profile_content_hash
    for factor in COMPACTION_REQUIRED:
        assert built.factor_token_estimates[factor] > 0
    trailer = built.messages[-1].text
    assert trailer.startswith("identity trailer")
    assert f"candidate_id: {built.candidate_id}" in trailer
    assert f"assembly_hash: {built.assembly_hash}" in trailer
    assert built.stable_prefix_hash
    assert built.body_hash
    assert built.final_pack_hash


def test_generated_at_is_the_newest_source_timestamp() -> None:
    built = orchestrator().handle(command())
    assert built.generated_at == "2026-10-17T12:00:00+00:00"


@pytest.mark.parametrize(
    "status",
    [
        EnumContextSourceStatus.UNAVAILABLE,
        EnumContextSourceStatus.STALE,
        EnumContextSourceStatus.DENIED,
        EnumContextSourceStatus.UNRESOLVED,
    ],
)
def test_required_tier_not_resolved_fails_required_factor_missing(
    status: EnumContextSourceStatus,
) -> None:
    bound = sources(open_work=FixtureSource(EnumContextFactor.OPEN_WORK, status))
    built = orchestrator(bound).handle(command())
    assert built.status is EnumContextPackOrchestratorStatus.FAILED
    assert built.failure_class is EnumContextPackFailure.REQUIRED_FACTOR_MISSING
    assert not built.messages
    tier = {t.factor: t for t in built.tier_statuses}[EnumContextFactor.OPEN_WORK]
    assert tier.required
    assert tier.status is status
    assert not tier.included


def test_unbound_required_source_fails_required_factor_missing() -> None:
    bound = sources()
    del bound[EnumContextFactor.ORCHESTRATION_STATE]
    built = orchestrator(bound).handle(command())
    assert built.failure_class is EnumContextPackFailure.REQUIRED_FACTOR_MISSING


def test_raising_source_is_a_tier_status_not_a_crash() -> None:
    class Broken(FixtureSource):
        def handle(
            self, request: ModelContextSourceRequest
        ) -> ModelContextSourceResult:
            raise RuntimeError("boom")

    bound = sources(open_work=Broken(EnumContextFactor.OPEN_WORK))
    built = orchestrator(bound).handle(command())
    assert built.failure_class is EnumContextPackFailure.REQUIRED_FACTOR_MISSING
    tier = {t.factor: t for t in built.tier_statuses}[EnumContextFactor.OPEN_WORK]
    assert tier.status is EnumContextSourceStatus.UNAVAILABLE
    assert tier.error_message == "RuntimeError: boom"


@pytest.mark.parametrize(
    "status", [EnumContextSourceStatus.UNAVAILABLE, EnumContextSourceStatus.STALE]
)
def test_optional_tier_not_resolved_is_omitted_and_listed(
    status: EnumContextSourceStatus,
) -> None:
    bound = sources(repo_state=FixtureSource(EnumContextFactor.REPO_STATE, status))
    built = orchestrator(bound).handle(command())
    assert built.status is EnumContextPackOrchestratorStatus.OK, built.errors
    tier = {t.factor: t for t in built.tier_statuses}[EnumContextFactor.REPO_STATE]
    assert not tier.required
    assert tier.status is status
    assert not tier.included
    assert EnumContextFactor.REPO_STATE not in built.factor_token_estimates
    assert all("[repo_state:" not in m.text for m in built.messages)


def test_lane_brief_builds_without_session(profiles_dir: Path) -> None:
    bound = sources()
    built = orchestrator(bound, profiles_dir).handle(
        command(profile_name="lane_brief", session_id=None, newest_turn=None)
    )
    assert built.status is EnumContextPackOrchestratorStatus.OK, built.errors
    assert EnumContextFactor.LANE_RULES in built.factor_token_estimates
    for request in bound[EnumContextFactor.LANE_RULES].requests:
        assert not any(scope.startswith("session:") for scope in request.scopes)


def test_unknown_profile_fails_profile_not_found() -> None:
    built = orchestrator().handle(command(profile_name="no_such_profile"))
    assert built.failure_class is EnumContextPackFailure.PROFILE_NOT_FOUND


def test_required_over_budget_fails_token_budget_exceeded() -> None:
    bound = sources(
        open_work=FixtureSource(EnumContextFactor.OPEN_WORK, text="x" * 4000)
    )
    built = orchestrator(bound).handle(command(token_budget_override=500))
    assert built.status is EnumContextPackOrchestratorStatus.FAILED
    assert built.failure_class is EnumContextPackFailure.TOKEN_BUDGET_EXCEEDED
    assert not built.messages


def test_optional_over_budget_is_excluded_lowest_precedence_first() -> None:
    bound = sources(
        conversation_tail=FixtureSource(
            EnumContextFactor.CONVERSATION_TAIL, text="t" * 2000
        )
    )
    built = orchestrator(bound).handle(command(token_budget_override=300))
    assert built.status is EnumContextPackOrchestratorStatus.OK, built.errors
    assert built.excluded_by_budget == (EnumContextFactor.CONVERSATION_TAIL,)
    assert EnumContextFactor.REPO_STATE in built.factor_token_estimates


def test_budget_override_may_only_lower_the_budget() -> None:
    built = orchestrator().handle(command(token_budget_override=10**6))
    assert built.failure_class is EnumContextPackFailure.INVALID_PROFILE


def test_caller_scope_and_access_reach_every_source_request_unchanged() -> None:
    scope = ModelContextCallerScope(
        repository="omnimarket", lane="cmod-ta8-packorch", ticket="OMN-21110"
    )
    bound = sources()
    built = orchestrator(bound).handle(command(caller_scope=scope))
    assert built.status is EnumContextPackOrchestratorStatus.OK, built.errors
    asked = [r for s in bound.values() for r in s.requests]
    # Every factor the profile selects and the overlay binds is asked exactly once.
    assert sorted(r.factor.value for r in asked) == sorted(
        f.value
        for f in (
            *COMPACTION_REQUIRED,
            EnumContextFactor.REPO_STATE,
            EnumContextFactor.CONVERSATION_TAIL,
        )
    )
    assert all(r.caller_scope == scope for r in asked)
    assert all(r.access.tenant_id == "tenant-a" for r in asked)


def test_task_kind_unknown_when_rule_descriptor_absent(profiles_dir: Path) -> None:
    bound = sources()
    built = orchestrator(bound, profiles_dir).handle(
        command(
            profile_name="lane_brief",
            session_id=None,
            newest_turn=None,
            caller_scope=ModelContextCallerScope(repository="omnimarket"),
        )
    )
    assert built.task_kind == TASK_KIND_UNKNOWN
    assert all(
        r.task_kind == TASK_KIND_UNKNOWN for s in bound.values() for r in s.requests
    )
    # plan_excerpts is named by a rule that did not match: it is withheld, never read.
    assert bound[EnumContextFactor.PLAN_EXCERPTS].requests == []


def test_task_kind_matches_a_supplied_descriptor(profiles_dir: Path) -> None:
    bound = sources()
    built = orchestrator(bound, profiles_dir).handle(
        command(
            profile_name="lane_brief",
            session_id=None,
            newest_turn=None,
            caller_scope=ModelContextCallerScope(node_archetype="orchestrator"),
        )
    )
    assert built.task_kind == "node_build"
    assert [r.task_kind for r in bound[EnumContextFactor.PLAN_EXCERPTS].requests] == [
        "node_build"
    ]


def test_same_fixture_twice_returns_the_same_identities() -> None:
    first = orchestrator().handle(command())
    second = orchestrator().handle(command())
    assert first.status is EnumContextPackOrchestratorStatus.OK
    for field in (
        "candidate_id",
        "assembly_hash",
        "final_pack_hash",
        "body_hash",
        "stable_prefix_hash",
    ):
        assert getattr(first, field) == getattr(second, field), field


def test_changed_profile_file_changes_candidate(profiles_dir: Path) -> None:
    before = orchestrator(profiles_dir=profiles_dir).handle(command())
    path = profiles_dir / "compaction_orchestrator.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "token_budget: 16000", "token_budget: 15000"
        ),
        encoding="utf-8",
    )
    after = orchestrator(profiles_dir=profiles_dir).handle(command())
    assert before.profile_content_hash != after.profile_content_hash
    assert before.candidate_id != after.candidate_id


def test_bumped_render_contract_changes_candidate() -> None:
    before = orchestrator().handle(command())
    after = orchestrator(render_contract_version="context-pack-render/2").handle(
        command()
    )
    assert before.candidate_id != after.candidate_id


def test_handler_change_under_unchanged_contract_changes_candidate() -> None:
    before = orchestrator().handle(command())
    after = orchestrator(
        identity={"omnimarket.version": "0.0.0", "code.fixture": "b" * 64}
    ).handle(command())
    assert before.candidate_id != after.candidate_id


def test_default_implementation_identity_digests_handler_code() -> None:
    identity = default_implementation_identity()
    for node in (
        "node_context_pack_orchestrator",
        "node_context_pack_builder_compute",
        "node_context_pack_render_compute",
        "node_context_profile_resolver_effect",
    ):
        assert len(identity[f"code.{node}"]) == 64


def test_changed_source_content_changes_candidate_and_hashes() -> None:
    before = orchestrator().handle(command())
    bound = sources(
        open_work=FixtureSource(EnumContextFactor.OPEN_WORK, text="another claim")
    )
    after = orchestrator(bound).handle(command())
    assert before.candidate_id != after.candidate_id
    assert before.assembly_hash != after.assembly_hash


def test_payload_refuses_a_tenant_field() -> None:
    with pytest.raises(ValidationError):
        ModelContextPackRequest.model_validate(
            {"profile_name": "compaction_orchestrator", "tenant_id": "tenant-b"}
        )


def test_pinned_mode_requires_its_manifest_case() -> None:
    with pytest.raises(ValidationError):
        ModelContextPackRequest(profile_name="lane_brief", mode="pinned")
