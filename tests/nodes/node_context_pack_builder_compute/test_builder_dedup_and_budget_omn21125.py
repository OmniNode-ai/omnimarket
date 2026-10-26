"""OMN-21125 (TC.2 builder half): content dedup and optional exclusion by budget."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_failure import EnumContextPackFailure
from omnibase_core.enums.enum_context_pack_provenance import (
    EnumContextPackProvenance,
)

from omnimarket.events.context_roi import EnumFailureStage
from omnimarket.nodes.node_context_pack_builder_compute.handlers.handler_context_pack_builder import (
    HandlerContextPackBuilder,
)
from omnimarket.nodes.node_context_roi_runner.handlers.handler_context_roi_runner import (
    _build_context_pack,
)
from omnimarket.pack.model_context_pack_artifact import (
    ModelContextPackArtifact,
)
from omnimarket.pack.model_context_pack_builder_request import (
    ModelContextPackBuilderRequest,
)
from omnimarket.pack.model_context_pack_builder_result import (
    EnumContextPackBuilderStatus,
    ModelContextPackBuilderResult,
)
from omnimarket.pack.model_context_profile import (
    ModelContextProfile,
)

GOLDEN = EnumContextFactor.GOLDEN_CHAIN
EXEMPLAR = EnumContextFactor.EXEMPLAR
FAILURES = EnumContextFactor.LOCAL_FAILURES


def _profile(**overrides: object) -> ModelContextProfile:
    values: dict[str, object] = {
        "model_id": "glm-4.5",
        "required_factors": (GOLDEN,),
        "optional_factors": (EXEMPLAR, FAILURES),
        "factor_precedence": (GOLDEN, EXEMPLAR, FAILURES),
        "token_budget": 100,
        "profile_version": "2026.10.26",
    }
    values.update(overrides)
    return ModelContextProfile(**values)


def _artifact(
    factor: EnumContextFactor,
    content: str,
    *,
    tokens: int = 10,
    source_hash: str | None = None,
) -> ModelContextPackArtifact:
    return ModelContextPackArtifact(
        factor=factor,
        content=content,
        token_estimate=tokens,
        provenance=EnumContextPackProvenance.GENERATED,
        source_artifact_hash=source_hash or f"sha-{factor.value}-{tokens}",
        source_ticket_id="OMN-21125",
        source_contract_hash="contract-sha",
        source_run_id="run-1",
    )


def _build(
    *artifacts: ModelContextPackArtifact,
    profile: ModelContextProfile | None = None,
) -> ModelContextPackBuilderResult:
    return HandlerContextPackBuilder().handle(
        ModelContextPackBuilderRequest(
            contract_hash="contract-sha",
            generated_at="2026-10-26T07:00:00Z",
            profile=profile or _profile(),
            artifacts=artifacts,
        )
    )


def _factors(result: ModelContextPackBuilderResult) -> tuple[EnumContextFactor, ...]:
    assert result.context_pack is not None
    return tuple(chunk.factor for chunk in result.context_pack.chunks)


@pytest.mark.unit
class TestContentDedup:
    def test_equal_content_yields_one_chunk_and_one_dropped_duplicate(self) -> None:
        result = _build(
            _artifact(GOLDEN, "same", source_hash="a"),
            _artifact(GOLDEN, "same", source_hash="b"),
        )

        assert result.status == EnumContextPackBuilderStatus.OK
        assert result.context_pack is not None
        assert len(result.context_pack.chunks) == 1
        assert result.context_pack.chunks[0].source_artifact_hash == "a"
        assert len(result.dropped_duplicates) == 1
        dropped = result.dropped_duplicates[0]
        assert dropped.factor == GOLDEN
        assert dropped.source_artifact_hash == "b"
        assert dropped.kept_factor == GOLDEN
        assert dropped.kept_source_artifact_hash == "a"
        assert len(dropped.content_sha256) == 64

    def test_dedup_compares_normalised_content(self) -> None:
        result = _build(
            _artifact(GOLDEN, "chain"),
            _artifact(EXEMPLAR, "line one\nline two", source_hash="ex"),
            _artifact(FAILURES, "line one  \r\nline two\n", source_hash="lf"),
        )

        assert result.status == EnumContextPackBuilderStatus.OK
        assert _factors(result) == (GOLDEN, EXEMPLAR)
        assert [d.source_artifact_hash for d in result.dropped_duplicates] == ["lf"]
        assert result.dropped_duplicates[0].kept_source_artifact_hash == "ex"

    def test_dedup_keeps_the_higher_authority_copy(self) -> None:
        # Precedence puts the optional exemplar ahead of the required golden
        # chain; the required copy still survives because it carries authority.
        profile = _profile(factor_precedence=(EXEMPLAR, GOLDEN, FAILURES))
        result = _build(
            _artifact(EXEMPLAR, "shared text", source_hash="ex"),
            _artifact(GOLDEN, "shared text", source_hash="gc"),
            profile=profile,
        )

        assert result.status == EnumContextPackBuilderStatus.OK
        assert _factors(result) == (GOLDEN,)
        assert len(result.dropped_duplicates) == 1
        dropped = result.dropped_duplicates[0]
        assert (dropped.factor, dropped.source_artifact_hash) == (EXEMPLAR, "ex")
        assert (dropped.kept_factor, dropped.kept_source_artifact_hash) == (
            GOLDEN,
            "gc",
        )

    def test_a_dropped_duplicate_is_not_charged_to_the_budget(self) -> None:
        result = _build(
            _artifact(GOLDEN, "same", tokens=60, source_hash="a"),
            _artifact(GOLDEN, "same", tokens=60, source_hash="b"),
        )

        assert result.status == EnumContextPackBuilderStatus.OK
        assert result.context_pack is not None
        assert result.context_pack.total_token_estimate == 60

    def test_distinct_content_drops_nothing(self) -> None:
        result = _build(_artifact(GOLDEN, "chain"), _artifact(EXEMPLAR, "exemplar"))

        assert result.status == EnumContextPackBuilderStatus.OK
        assert result.dropped_duplicates == ()
        assert result.excluded_by_budget == ()
        assert result.warnings == ()


@pytest.mark.unit
class TestOptionalExclusionByBudget:
    def test_optional_artifacts_that_do_not_fit_are_listed_in_reverse_precedence(
        self,
    ) -> None:
        result = _build(
            _artifact(GOLDEN, "chain", tokens=80),
            _artifact(EXEMPLAR, "exemplar", tokens=30),
            _artifact(FAILURES, "failures", tokens=25),
        )

        assert result.status == EnumContextPackBuilderStatus.OK
        assert _factors(result) == (GOLDEN,)
        assert result.context_pack is not None
        assert result.context_pack.total_token_estimate == 80
        assert [e.factor for e in result.excluded_by_budget] == [FAILURES, EXEMPLAR]
        assert [e.token_estimate for e in result.excluded_by_budget] == [25, 30]
        assert [e.source_artifact_hash for e in result.excluded_by_budget] == [
            "sha-local_failures-25",
            "sha-exemplar-30",
        ]

    def test_a_smaller_optional_artifact_behind_a_large_one_is_admitted(
        self,
    ) -> None:
        result = _build(
            _artifact(GOLDEN, "chain", tokens=50),
            _artifact(EXEMPLAR, "exemplar", tokens=60),
            _artifact(FAILURES, "failures", tokens=40),
        )

        assert result.status == EnumContextPackBuilderStatus.OK
        assert _factors(result) == (GOLDEN, FAILURES)
        assert result.context_pack is not None
        assert result.context_pack.total_token_estimate == 90
        assert [e.factor for e in result.excluded_by_budget] == [EXEMPLAR]

    def test_required_artifacts_over_budget_still_fail_and_are_named(self) -> None:
        result = _build(
            _artifact(GOLDEN, "chain", tokens=101, source_hash="gc-big"),
            _artifact(EXEMPLAR, "exemplar", tokens=5),
        )

        assert result.status == EnumContextPackBuilderStatus.FAILED
        assert result.failure_class == EnumContextPackFailure.TOKEN_BUDGET_EXCEEDED
        assert result.context_pack is None
        assert result.excluded_by_budget == ()
        assert any("gc-big" in error for error in result.errors)

    def test_a_required_artifact_is_never_listed_as_excluded(self) -> None:
        result = _build(
            _artifact(GOLDEN, "chain one", tokens=60, source_hash="gc-1"),
            _artifact(GOLDEN, "chain two", tokens=40, source_hash="gc-2"),
            _artifact(EXEMPLAR, "exemplar", tokens=1),
        )

        assert result.status == EnumContextPackBuilderStatus.OK
        assert _factors(result) == (GOLDEN, GOLDEN)
        assert [e.factor for e in result.excluded_by_budget] == [EXEMPLAR]


@pytest.mark.unit
def test_roi_runner_still_reports_budget_fail_when_an_arm_does_not_fit() -> None:
    # AC1: the ROI runner measures whole arms; an arm whose factors do not fit
    # stays a budget failure rather than a silently smaller arm. Require the
    # successful builder's exclusion path, not its former whole-pack rejection.
    context_text, pack_hash, warnings, stage = _build_context_pack(
        ("golden_chain", "exemplar"),
        {"golden_chain": "chain", "exemplar": "x" * 70_000},
        "contract-sha",
    )

    assert stage == EnumFailureStage.BUDGET_FAIL
    assert context_text == ""
    assert pack_hash == ""
    assert len(warnings) == 1
    assert warnings[0].startswith(
        "context-pack builder excluded 1 artifact(s) by budget: exemplar:"
    )


_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_context_pack_builder_compute/contract.yaml"
)


@pytest.mark.unit
def test_contract_declares_the_new_outputs_and_the_build_topics() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    completed = "onex.evt.omnimarket.context-pack-build-completed.v1"
    failed = "onex.evt.omnimarket.context-pack-build-failed.v1"

    assert {"dropped_duplicates", "excluded_by_budget", "warnings"} <= set(
        contract["outputs"]
    )
    assert contract["terminal_event"] == completed
    assert contract["event_bus"]["publish_topics"] == [completed, failed]
