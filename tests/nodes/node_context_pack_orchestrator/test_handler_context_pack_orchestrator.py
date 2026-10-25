# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerContextPackOrchestrator criteria (OMN-21110, plan task TA.8)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_failure import EnumContextPackFailure
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from omnibase_core.models.context.model_context_caller_scope import (
    ModelContextCallerScope,
)
from omnibase_core.models.context.model_context_source_entry import (
    ModelContextSourceEntry,
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
    HandlerContextPackOrchestrator,
    default_implementation_identity,
)
from omnimarket.nodes.node_context_pack_orchestrator.protocols import (
    ProtocolContextTierSource,
)
from omnimarket.nodes.node_context_profile_resolver_effect.handlers.handler_context_profile_resolver import (
    HandlerContextProfileResolver,
)
from omnimarket.nodes.node_session_context_sources_effect.handlers.handler_session_context_sources import (
    HandlerSessionContextSources,
)
from omnimarket.nodes.node_session_context_sources_effect.models import (
    EnumSessionSourceFailure,
)

from .conftest import (
    AS_OF,
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


@pytest.mark.parametrize("budget", [None, 300])
def test_tier_read_timings_survive_budget_selection(budget: int | None) -> None:
    bound = sources(
        open_work=ManySource(EnumContextFactor.OPEN_WORK, _open_work_rows()),
        conversation_tail=FixtureSource(
            EnumContextFactor.CONVERSATION_TAIL, text="t" * 2000
        ),
        tail_summary=FixtureSource(EnumContextFactor.TAIL_SUMMARY, text="summary"),
    )
    selected = (
        *COMPACTION_REQUIRED,
        EnumContextFactor.REPO_STATE,
        EnumContextFactor.CONVERSATION_TAIL,
        EnumContextFactor.TAIL_SUMMARY,
    )
    ticks = iter(
        value
        for index in range(len(selected))
        for value in (float(index), index + (index + 1) / 16)
    )
    built = HandlerContextPackOrchestrator(bound, clock=lambda: next(ticks)).handle(
        command(token_budget_override=budget)
    )
    assert built.status is EnumContextPackOrchestratorStatus.OK, built.errors
    tiers = {tier.factor: tier for tier in built.tier_statuses}
    for index, factor in enumerate(selected):
        assert tiers[factor].read_ms == (index + 1) * 62.5
    assert tiers[EnumContextFactor.TRANSCRIPT_EXTRACTS].read_ms is None
    assert built.model_section_ms() == tiers[EnumContextFactor.TAIL_SUMMARY].read_ms
    if budget is not None:
        assert not tiers[EnumContextFactor.TAIL_SUMMARY].included
        assert tiers[EnumContextFactor.OPEN_WORK].trimmed_entry_count > 0


def test_no_model_written_read_has_no_model_section_timing() -> None:
    built = orchestrator().handle(command())
    assert built.status is EnumContextPackOrchestratorStatus.OK, built.errors
    assert built.model_section_ms() is None


def test_raising_source_still_records_read_timing() -> None:
    class Broken(FixtureSource):
        def handle(
            self, request: ModelContextSourceRequest
        ) -> ModelContextSourceResult:
            raise RuntimeError("boom")

    bound = sources(open_work=Broken(EnumContextFactor.OPEN_WORK))
    ticks = iter(float(index) / 4 for index in range(12))
    built = HandlerContextPackOrchestrator(bound, clock=lambda: next(ticks)).handle(
        command()
    )
    assert built.failure_class is EnumContextPackFailure.REQUIRED_FACTOR_MISSING
    tier = next(
        t for t in built.tier_statuses if t.factor is EnumContextFactor.OPEN_WORK
    )
    assert tier.status is EnumContextSourceStatus.UNAVAILABLE
    assert tier.read_ms == 250.0


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


# OMN-21111: selection inside a required tier, and a revision per tier.


class ManySource(FixtureSource):
    """A source answering several entries, one per (text, as_of, source_ref)."""

    def __init__(
        self,
        factor: EnumContextFactor,
        rows: tuple[tuple[str, datetime, str], ...],
    ) -> None:
        super().__init__(factor)
        self.rows = rows

    def handle(self, request: ModelContextSourceRequest) -> ModelContextSourceResult:
        self.requests.append(request)
        return ModelContextSourceResult(
            status=EnumContextSourceStatus.RESOLVED,
            entries=tuple(
                ModelContextSourceEntry(
                    factor=self.factor,
                    text=text,
                    source_ref=ref,
                    text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    status=EnumContextSourceStatus.RESOLVED,
                    as_of=as_of,
                )
                for text, as_of, ref in self.rows
            ),
        )


def _open_work_rows(count: int = 5) -> tuple[tuple[str, datetime, str], ...]:
    # Each row is about 100 tokens; day N is newer than day N-1.
    return tuple(
        (
            f"2026-10-1{day}T00:00:00Z | MSG | lane=orchestrator-bb1a | day-{day} "
            + "w" * 360,
            datetime(2026, 10, 10 + day, tzinfo=UTC),
            f"row/{day}",
        )
        for day in range(1, count + 1)
    )


def _carried(built_text: str) -> set[str]:
    return {f"day-{day}" for day in range(1, 6) if f"day-{day} " in built_text}


def test_required_tier_over_budget_keeps_cited_then_newest() -> None:
    bound = sources(
        open_work=ManySource(EnumContextFactor.OPEN_WORK, _open_work_rows())
    )
    built = orchestrator(bound).handle(
        command(token_budget_override=300, cited_refs=("row/1",))
    )
    assert built.status is EnumContextPackOrchestratorStatus.OK, built.errors
    text = "\n".join(m.text for m in built.messages)
    assert _carried(text) == {"day-1", "day-5"}
    tier = {t.factor: t for t in built.tier_statuses}[EnumContextFactor.OPEN_WORK]
    assert tier.included
    assert tier.entry_count == 2
    assert tier.trimmed_entry_count == 3
    assert sum(built.factor_token_estimates.values()) <= 300


def test_required_tier_trim_cites_a_row_by_its_stamp() -> None:
    bound = sources(
        open_work=ManySource(EnumContextFactor.OPEN_WORK, _open_work_rows())
    )
    built = orchestrator(bound).handle(
        command(token_budget_override=300, cited_refs=("2026-10-12T00:00:00Z",))
    )
    assert built.status is EnumContextPackOrchestratorStatus.OK, built.errors
    text = "\n".join(m.text for m in built.messages)
    assert _carried(text) == {"day-2", "day-5"}


def test_required_tier_trim_without_citations_keeps_newest_first() -> None:
    bound = sources(
        open_work=ManySource(EnumContextFactor.OPEN_WORK, _open_work_rows())
    )
    built = orchestrator(bound).handle(command(token_budget_override=300))
    assert built.status is EnumContextPackOrchestratorStatus.OK, built.errors
    text = "\n".join(m.text for m in built.messages)
    assert _carried(text) == {"day-4", "day-5"}


def test_cited_entries_over_an_equal_share_build_when_the_budget_holds_them() -> None:
    bound = sources(
        open_work=ManySource(EnumContextFactor.OPEN_WORK, _open_work_rows()),
        orchestration_state=ManySource(
            EnumContextFactor.ORCHESTRATION_STATE,
            (
                ("s" * 400, AS_OF, "state/small"),
                ("s" * 2800, AS_OF, "state/large"),
            ),
        ),
    )
    budget = 800
    cited = tuple(f"row/{day}" for day in range(1, 6))
    built = orchestrator(bound).handle(
        command(token_budget_override=budget, cited_refs=cited)
    )
    assert built.status is EnumContextPackOrchestratorStatus.OK, built.errors
    text = "\n".join(m.text for m in built.messages)
    assert _carried(text) == {f"day-{day}" for day in range(1, 6)}
    assert sum(built.factor_token_estimates.values()) <= budget
    tier = {t.factor: t for t in built.tier_statuses}[
        EnumContextFactor.ORCHESTRATION_STATE
    ]
    assert tier.trimmed_entry_count == 1


def test_cited_entries_over_the_whole_budget_fail_naming_the_tiers() -> None:
    bound = sources(
        open_work=ManySource(EnumContextFactor.OPEN_WORK, _open_work_rows()),
        orchestration_state=FixtureSource(
            EnumContextFactor.ORCHESTRATION_STATE, text="s" * 1600
        ),
    )
    cited = (
        *(f"row/{day}" for day in range(1, 6)),
        "fixture/orchestration_state@0000000",
    )
    built = orchestrator(bound).handle(
        command(token_budget_override=800, cited_refs=cited)
    )
    assert built.status is EnumContextPackOrchestratorStatus.FAILED
    assert built.failure_class is EnumContextPackFailure.TOKEN_BUDGET_EXCEEDED
    assert any(
        "cited entries need" in error
        and error.endswith(": open_work, orchestration_state")
        for error in built.errors
    )


def test_cited_entries_over_the_tier_budget_fail_never_dropped() -> None:
    bound = sources(
        open_work=ManySource(EnumContextFactor.OPEN_WORK, _open_work_rows())
    )
    cited = tuple(f"row/{day}" for day in range(1, 6))
    built = orchestrator(bound).handle(
        command(token_budget_override=300, cited_refs=cited)
    )
    assert built.status is EnumContextPackOrchestratorStatus.FAILED
    assert built.failure_class is EnumContextPackFailure.TOKEN_BUDGET_EXCEEDED
    assert any("cited" in error for error in built.errors)


def test_operator_words_unrecorded_is_carried_whole_never_trimmed() -> None:
    rows = tuple(
        (f"prompt-{n} " + "p" * 400, AS_OF, f"prompt/{n}") for n in range(1, 4)
    )
    bound = sources(
        operator_words_unrecorded=ManySource(
            EnumContextFactor.OPERATOR_WORDS_UNRECORDED, rows
        )
    )
    built = orchestrator(bound).handle(command(token_budget_override=200))
    assert built.status is EnumContextPackOrchestratorStatus.FAILED
    assert built.failure_class is EnumContextPackFailure.TOKEN_BUDGET_EXCEEDED


def test_each_tier_reads_its_own_revision() -> None:
    bound = sources()
    revisions = {
        EnumContextFactor.OPERATOR_RULINGS: "2026-10-19T07:36:00Z",
        EnumContextFactor.OPERATOR_WORDS_UNRECORDED: "a" * 64,
    }
    built = orchestrator(bound).handle(command(tier_revisions=revisions))
    assert built.status is EnumContextPackOrchestratorStatus.OK, built.errors
    for factor, source in bound.items():
        for request in source.requests:
            assert request.revision == revisions.get(factor, "turn-9")


def test_tier_revisions_change_the_candidate() -> None:
    plain = orchestrator().handle(command())
    pinned = orchestrator().handle(
        command(tier_revisions={EnumContextFactor.OPERATOR_RULINGS: "r-1"})
    )
    assert plain.candidate_id != pinned.candidate_id


# OMN-17427 (TA.9 seam): a pinned build names its replay case the way the session
# source reads it, and carries exactly one mode scope.


def _scopes_sent(**fields: object) -> tuple[str, ...]:
    bound = sources()
    orchestrator(bound).handle(command(**fields))
    sent = {request.scopes for source in bound.values() for request in source.requests}
    assert len(sent) == 1
    return sent.pop()


def test_a_pinned_build_sends_the_case_scope_and_no_session_scope() -> None:
    scopes = _scopes_sent(mode="pinned", manifest_case="case-a")
    assert "case:case-a" in scopes
    assert not [s for s in scopes if s.startswith(("session:", "manifest_case:"))]


def test_a_live_build_sends_the_session_scope_and_no_case_scope() -> None:
    scopes = _scopes_sent()
    assert "session:session-1" in scopes
    assert not [s for s in scopes if s.startswith(("case:", "manifest_case:"))]


class _NoProjectionRows:
    """A live session source reader over empty projections."""

    def read_prompt_rows(
        self, *, session_id: str, until: datetime | None
    ) -> tuple[dict[str, object], ...]:
        return ()

    def read_raw_rows(self, *, until: datetime | None) -> tuple[str, ...]:
        return ()


@pytest.mark.parametrize(
    ("fields", "mode_scope"),
    [
        ({"mode": "pinned", "manifest_case": "case-a"}, "case:case-a"),
        ({}, "session:session-1"),
    ],
    ids=["pinned", "live"],
)
def test_the_session_source_accepts_the_scopes_the_orchestrator_sends(
    fields: dict[str, object], mode_scope: str
) -> None:
    scopes = _scopes_sent(**fields)
    assert mode_scope in scopes
    result = HandlerSessionContextSources(
        env={},
        tenant_ids=(command().access.tenant_id,),
        content_reader=_NoProjectionRows(),
        ledger_reader=_NoProjectionRows(),
    ).handle(
        ModelContextSourceRequest(
            factor=EnumContextFactor.OPERATOR_WORDS_UNRECORDED,
            scopes=scopes,
            access=command().access,
            revision="r",
        )
    )
    assert result.status is not EnumContextSourceStatus.DENIED
    assert not (result.error_message or "").startswith(
        EnumSessionSourceFailure.SCOPE_INVALID.value
    ), result.error_message


@pytest.mark.parametrize("days", [(1, 2, 3, 4, 5), (5, 4, 3, 2, 1), (3, 1, 5, 2, 4)])
def test_a_tier_renders_its_entries_in_source_order(days: tuple[int, ...]) -> None:
    """The order a source answers in is the order the pack carries, not hash order."""
    rows = dict(enumerate(_open_work_rows(), start=1))
    bound = sources(
        open_work=ManySource(
            EnumContextFactor.OPEN_WORK, tuple(rows[day] for day in days)
        )
    )
    built = orchestrator(bound).handle(command())
    assert built.status is EnumContextPackOrchestratorStatus.OK, built.errors
    text = "\n".join(m.text for m in built.messages)
    positions = [text.index(f"day-{day} ") for day in days]
    assert positions == sorted(positions)
