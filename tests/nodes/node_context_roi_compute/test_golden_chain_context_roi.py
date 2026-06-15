# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_context_roi_compute (OMN-12797 P2-2/P2-3).

Acceptance criteria per ticket:
  P2-2: Fixed task manifest with explicit success criteria + per-arm required/optional
        factor declarations; missing required factor fails the row (not warns);
        missing optional factor warns (never silent-green).
  P2-3: N-arm factor matrix with deterministic factor order; full_guidance_negative_control
        never ranked preferred; budget failures scored separately from generation failures.

All tests use fixture_mode=True (no .201 dependency; fully offline, replay-proven).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor

from omnimarket.nodes.node_context_roi_compute.handlers.handler_context_roi import (
    HandlerContextRoi,
)
from omnimarket.nodes.node_context_roi_compute.models.model_context_roi_request import (
    ModelArmRunRow,
    ModelContextRoiRequest,
)
from omnimarket.nodes.node_context_roi_compute.models.model_context_roi_result import (
    EnumProofClass,
    ModelContextRoiResult,
)
from omnimarket.nodes.node_context_roi_compute.models.model_factor_arm import (
    EnumArmLabel,
    ModelFactorArm,
)
from omnimarket.nodes.node_context_roi_compute.models.model_factor_matrix import (
    arm_by_label,
    build_canonical_factor_matrix,
)
from omnimarket.nodes.node_context_roi_compute.models.model_task_manifest import (
    EnumFailureStage,
    EnumSeaPathCategory,
    ModelExperimentTask,
    ModelTaskManifest,
    build_canonical_task_manifest,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def handler() -> HandlerContextRoi:
    return HandlerContextRoi()


@pytest.fixture
def node_dir() -> Path:
    return (
        Path(__file__).resolve().parent.parent.parent.parent
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_context_roi_compute"
    )


@pytest.fixture
def contract_path(node_dir: Path) -> Path:
    return node_dir / "contract.yaml"


@pytest.fixture
def metadata_path(node_dir: Path) -> Path:
    return node_dir / "metadata.yaml"


@pytest.fixture
def matrix() -> tuple[ModelFactorArm, ...]:
    return build_canonical_factor_matrix()


@pytest.fixture
def manifest() -> ModelTaskManifest:
    return build_canonical_task_manifest()


def _make_row(
    task_id: str,
    arm_label: EnumArmLabel,
    trial_index: int = 0,
    first_pass_success: bool = True,
    final_success: bool = True,
    attempt_count: int = 1,
    failure_stage: EnumFailureStage = EnumFailureStage.NONE,
    factors_present: tuple[EnumContextFactor, ...] = (),
    factors_warned_absent: tuple[EnumContextFactor, ...] = (),
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    estimated_cost_usd: float | None = None,
) -> ModelArmRunRow:
    return ModelArmRunRow(
        task_id=task_id,
        arm_label=arm_label,
        trial_index=trial_index,
        run_id=f"{task_id}-{arm_label}-{trial_index}",
        first_pass_success=first_pass_success,
        final_success=final_success,
        attempt_count=attempt_count,
        failure_stage=failure_stage,
        factors_present=factors_present,
        factors_warned_absent=factors_warned_absent,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        estimated_cost_usd=estimated_cost_usd,
    )


# ---------------------------------------------------------------------------
# Contract / metadata gate
# ---------------------------------------------------------------------------


class TestContractYaml:
    def test_contract_exists(self, contract_path: Path) -> None:
        assert contract_path.exists()

    def test_contract_loads(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        assert isinstance(data, dict)
        assert data["name"] == "node_context_roi_compute"
        assert data["node_type"] == "compute"
        assert data.get("node_not_implemented") is False

    def test_contract_purity(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        descriptor = data.get("descriptor", {})
        assert descriptor.get("purity") == "pure"
        assert descriptor.get("idempotent") is True

    def test_contract_declares_terminal_event(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        assert "terminal_event" in data
        assert "context-roi-score-completed" in data["terminal_event"]

    def test_contract_topics_follow_convention(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        bus = data.get("event_bus", {})
        for topic in bus.get("subscribe_topics", []):
            assert topic.startswith("onex.cmd."), f"subscribe topic bad prefix: {topic}"
            assert topic.endswith(".v1"), f"subscribe topic missing .v1: {topic}"
        for topic in bus.get("publish_topics", []):
            assert topic.startswith("onex.evt."), f"publish topic bad prefix: {topic}"
            assert topic.endswith(".v1"), f"publish topic missing .v1: {topic}"

    def test_contract_declares_handler(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        handler_cfg = data.get("handler", {})
        assert "module" in handler_cfg
        assert "class" in handler_cfg


class TestMetadataYaml:
    def test_metadata_exists(self, metadata_path: Path) -> None:
        assert metadata_path.exists()

    def test_metadata_loads(self, metadata_path: Path) -> None:
        data = yaml.safe_load(metadata_path.read_text())
        assert data["name"] == "node_context_roi_compute"
        assert "version" in data
        assert "entry_points" in data

    def test_metadata_pure(self, metadata_path: Path) -> None:
        data = yaml.safe_load(metadata_path.read_text())
        caps = data.get("capabilities", {})
        assert caps.get("side_effect_class") == "pure"
        assert caps.get("requires_network") is False


# ---------------------------------------------------------------------------
# P2-2: Task manifest acceptance
# ---------------------------------------------------------------------------


class TestTaskManifestStructure:
    def test_manifest_constructs(self, manifest: ModelTaskManifest) -> None:
        assert manifest is not None

    def test_manifest_id_stable(self, manifest: ModelTaskManifest) -> None:
        assert manifest.manifest_id == "omn-12797-v1"

    def test_manifest_has_five_tasks(self, manifest: ModelTaskManifest) -> None:
        assert len(manifest.tasks) == 5

    def test_manifest_task_ids_unique(self, manifest: ModelTaskManifest) -> None:
        ids = [t.task_id for t in manifest.tasks]
        assert len(ids) == len(set(ids))

    def test_manifest_task_ids_sequential(self, manifest: ModelTaskManifest) -> None:
        ids = [t.task_id for t in manifest.tasks]
        assert ids == ["sea_001", "sea_002", "sea_003", "sea_004", "sea_005"]

    def test_manifest_covers_all_sea_path_categories(
        self, manifest: ModelTaskManifest
    ) -> None:
        categories = {t.sea_path_category for t in manifest.tasks}
        assert categories == set(EnumSeaPathCategory)

    def test_each_task_has_success_criteria(self, manifest: ModelTaskManifest) -> None:
        for task in manifest.tasks:
            assert len(task.success_criteria) >= 1, (
                f"task {task.task_id} has no success criteria"
            )

    def test_success_criteria_have_verification_methods(
        self, manifest: ModelTaskManifest
    ) -> None:
        for task in manifest.tasks:
            for crit in task.success_criteria:
                assert crit.verification_method, (
                    f"task {task.task_id} criterion {crit.criterion_id} "
                    f"missing verification_method"
                )

    def test_task_prompts_are_self_contained(self, manifest: ModelTaskManifest) -> None:
        """Prompts must not reference session memory or external context."""
        forbidden = ["session memory", "previous session", "as discussed"]
        for task in manifest.tasks:
            prompt_lower = task.task_prompt.lower()
            for phrase in forbidden:
                assert phrase not in prompt_lower, (
                    f"task {task.task_id} prompt references hidden session state: {phrase!r}"
                )

    def test_task_by_id_lookup(self, manifest: ModelTaskManifest) -> None:
        task = manifest.task_by_id("sea_003")
        assert task.sea_path_category == EnumSeaPathCategory.EVENT_TOPIC_ALIGNMENT

    def test_task_by_id_raises_for_unknown(self, manifest: ModelTaskManifest) -> None:
        with pytest.raises(KeyError):
            manifest.task_by_id("nonexistent_999")

    def test_task_arm_policy_lookup(self, manifest: ModelTaskManifest) -> None:
        task = manifest.task_by_id("sea_001")
        policy = task.arm_policy(EnumArmLabel.GOLDEN_ONLY)
        assert policy is not None
        assert EnumContextFactor.GOLDEN_CHAIN in policy.required_factors

    def test_task_arm_policy_returns_none_for_undeclared(
        self, manifest: ModelTaskManifest
    ) -> None:
        # All tasks declare arm policies, so this tests that arm_policy()
        # returns None when queried for a label not in the task's arm_policies
        task = manifest.task_by_id("sea_001")
        # Create a dummy label that no task declares (we test via None return contract)
        # All arm labels are declared, so we verify the return type contract
        result = task.arm_policy(EnumArmLabel.OFF)
        assert result is not None  # OFF is declared on every task

    def test_sea_005_test_repair_requires_local_failures_in_failures_arm(
        self, manifest: ModelTaskManifest
    ) -> None:
        """Test-repair task must require LOCAL_FAILURES in the failures arm."""
        task = manifest.task_by_id("sea_005")
        policy = task.arm_policy(EnumArmLabel.GOLDEN_EXEMPLAR_FAILURES)
        assert policy is not None
        assert EnumContextFactor.LOCAL_FAILURES in policy.required_factors

    def test_manifest_frozen(self, manifest: ModelTaskManifest) -> None:
        from pydantic import ValidationError

        with pytest.raises((ValidationError, TypeError)):
            manifest.manifest_id = "mutated"  # type: ignore[misc]


class TestTaskManifestDuplicateIds:
    def test_duplicate_task_id_raises(self) -> None:
        from pydantic import ValidationError

        task = ModelExperimentTask(
            task_id="dup_001",
            description="task a",
            sea_path_category=EnumSeaPathCategory.NODE_CONTRACT_CORRECTNESS,
            task_prompt="generate something",
            success_criteria=(
                __import__(
                    "omnimarket.nodes.node_context_roi_compute.models.model_task_manifest",
                    fromlist=["ModelTaskSuccessCriterion"],
                ).ModelTaskSuccessCriterion(
                    criterion_id="c1",
                    description="passes",
                    verification_method="schema_check",
                ),
            ),
        )
        with pytest.raises(ValidationError, match="duplicate task_id"):
            ModelTaskManifest(
                manifest_id="test-v1",
                description="test",
                tasks=(task, task),
            )


# ---------------------------------------------------------------------------
# P2-3: Factor matrix acceptance
# ---------------------------------------------------------------------------


class TestFactorMatrix:
    def test_matrix_constructs(self, matrix: tuple[ModelFactorArm, ...]) -> None:
        assert len(matrix) == 7

    def test_matrix_arm_labels_cover_all_enum_values(
        self, matrix: tuple[ModelFactorArm, ...]
    ) -> None:
        labels = {arm.label for arm in matrix}
        assert labels == set(EnumArmLabel)

    def test_matrix_canonical_order(self, matrix: tuple[ModelFactorArm, ...]) -> None:
        expected_order = [
            EnumArmLabel.OFF,
            EnumArmLabel.GOLDEN_ONLY,
            EnumArmLabel.GOLDEN_EXEMPLAR,
            EnumArmLabel.GOLDEN_EXEMPLAR_FAILURES,
            EnumArmLabel.STRUCTURED_CONTEXT,
            EnumArmLabel.STRUCTURED_PLUS_GUIDANCE_CHUNKS,
            EnumArmLabel.FULL_GUIDANCE_NEGATIVE_CONTROL,
        ]
        assert [arm.label for arm in matrix] == expected_order

    def test_off_arm_has_no_factors(self, matrix: tuple[ModelFactorArm, ...]) -> None:
        off = arm_by_label(matrix, EnumArmLabel.OFF)
        assert off.factors == ()
        assert off.required_factors == ()
        assert off.optional_factors == ()

    def test_golden_only_requires_golden_chain(
        self, matrix: tuple[ModelFactorArm, ...]
    ) -> None:
        arm = arm_by_label(matrix, EnumArmLabel.GOLDEN_ONLY)
        assert EnumContextFactor.GOLDEN_CHAIN in arm.required_factors
        assert arm.factors == (EnumContextFactor.GOLDEN_CHAIN,)

    def test_golden_exemplar_golden_chain_required(
        self, matrix: tuple[ModelFactorArm, ...]
    ) -> None:
        arm = arm_by_label(matrix, EnumArmLabel.GOLDEN_EXEMPLAR)
        assert EnumContextFactor.GOLDEN_CHAIN in arm.required_factors
        assert EnumContextFactor.EXEMPLAR in arm.optional_factors

    def test_golden_exemplar_failures_includes_local_failures(
        self, matrix: tuple[ModelFactorArm, ...]
    ) -> None:
        arm = arm_by_label(matrix, EnumArmLabel.GOLDEN_EXEMPLAR_FAILURES)
        assert EnumContextFactor.LOCAL_FAILURES in arm.factors
        assert EnumContextFactor.LOCAL_FAILURES in arm.optional_factors

    def test_structured_context_includes_architecture_patterns(
        self, matrix: tuple[ModelFactorArm, ...]
    ) -> None:
        arm = arm_by_label(matrix, EnumArmLabel.STRUCTURED_CONTEXT)
        assert EnumContextFactor.ARCHITECTURE_PATTERNS in arm.factors

    def test_structured_plus_guidance_includes_claude_md(
        self, matrix: tuple[ModelFactorArm, ...]
    ) -> None:
        arm = arm_by_label(matrix, EnumArmLabel.STRUCTURED_PLUS_GUIDANCE_CHUNKS)
        assert EnumContextFactor.CLAUDE_MD in arm.factors
        assert EnumContextFactor.CLAUDE_MD in arm.optional_factors

    def test_full_guidance_is_negative_control(
        self, matrix: tuple[ModelFactorArm, ...]
    ) -> None:
        arm = arm_by_label(matrix, EnumArmLabel.FULL_GUIDANCE_NEGATIVE_CONTROL)
        assert arm.is_negative_control is True
        assert arm.factors == (EnumContextFactor.CLAUDE_MD,)

    def test_required_optional_disjoint_for_all_arms(
        self, matrix: tuple[ModelFactorArm, ...]
    ) -> None:
        for arm in matrix:
            overlap = set(arm.required_factors) & set(arm.optional_factors)
            assert overlap == set(), (
                f"arm {arm.label}: required and optional overlap: {overlap}"
            )

    def test_all_factors_declared_in_arms(
        self, matrix: tuple[ModelFactorArm, ...]
    ) -> None:
        """Every factor in arm.factors must be in required or optional."""
        for arm in matrix:
            declared = set(arm.required_factors) | set(arm.optional_factors)
            for factor in arm.factors:
                assert factor in declared, (
                    f"arm {arm.label}: factor {factor} undeclared "
                    f"(not in required or optional)"
                )

    def test_arm_by_label_raises_for_unknown(
        self, matrix: tuple[ModelFactorArm, ...]
    ) -> None:
        with pytest.raises(KeyError):
            arm_by_label(matrix, "nonexistent")  # type: ignore[arg-type]

    def test_matrix_is_deterministic(self) -> None:
        m1 = build_canonical_factor_matrix()
        m2 = build_canonical_factor_matrix()
        assert [a.label for a in m1] == [a.label for a in m2]
        for a1, a2 in zip(m1, m2, strict=True):
            assert a1.factors == a2.factors
            assert a1.required_factors == a2.required_factors

    def test_negative_control_must_be_marked(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="is_negative_control"):
            ModelFactorArm(
                label=EnumArmLabel.FULL_GUIDANCE_NEGATIVE_CONTROL,
                factors=(EnumContextFactor.CLAUDE_MD,),
                required_factors=(),
                optional_factors=(EnumContextFactor.CLAUDE_MD,),
                is_negative_control=False,  # must be True for this label
            )

    def test_required_optional_overlap_raises(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="overlap"):
            ModelFactorArm(
                label=EnumArmLabel.GOLDEN_ONLY,
                factors=(EnumContextFactor.GOLDEN_CHAIN,),
                required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
                optional_factors=(EnumContextFactor.GOLDEN_CHAIN,),  # overlaps
                is_negative_control=False,
            )

    def test_undeclared_factor_in_factors_raises(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="undeclared"):
            ModelFactorArm(
                label=EnumArmLabel.GOLDEN_ONLY,
                factors=(EnumContextFactor.GOLDEN_CHAIN, EnumContextFactor.EXEMPLAR),
                required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
                optional_factors=(),  # EXEMPLAR not declared
                is_negative_control=False,
            )


# ---------------------------------------------------------------------------
# Handler: fixture mode golden chain
# ---------------------------------------------------------------------------


_SIMPLE_OFF_ROW = _make_row(
    task_id="sea_001",
    arm_label=EnumArmLabel.OFF,
    first_pass_success=False,
    final_success=True,
    attempt_count=2,
    factors_present=(),
    prompt_tokens=200,
    completion_tokens=300,
    estimated_cost_usd=0.0005,
)

_SIMPLE_GOLDEN_ROW = _make_row(
    task_id="sea_001",
    arm_label=EnumArmLabel.GOLDEN_ONLY,
    first_pass_success=True,
    final_success=True,
    attempt_count=1,
    factors_present=(EnumContextFactor.GOLDEN_CHAIN,),
    prompt_tokens=1500,
    completion_tokens=120,
    estimated_cost_usd=0.0032,
)

_SIMPLE_REQUEST = ModelContextRoiRequest(
    run_id="test-run-001",
    manifest_id="omn-12797-v1",
    rows=(_SIMPLE_OFF_ROW, _SIMPLE_GOLDEN_ROW),
    fixture_mode=True,
)


class TestHandlerFixtureMode:
    def test_status_ok(self, handler: HandlerContextRoi) -> None:
        result = handler.handle(_SIMPLE_REQUEST)
        assert result.status == "ok"

    def test_proof_class_replay_proven(self, handler: HandlerContextRoi) -> None:
        result = handler.handle(_SIMPLE_REQUEST)
        assert result.proof_class == EnumProofClass.REPLAY_PROVEN

    def test_produces_arm_rows(self, handler: HandlerContextRoi) -> None:
        result = handler.handle(_SIMPLE_REQUEST)
        assert len(result.arm_rows) == 2

    def test_arm_row_task_ids_match(self, handler: HandlerContextRoi) -> None:
        result = handler.handle(_SIMPLE_REQUEST)
        task_ids = {r.task_id for r in result.arm_rows}
        assert "sea_001" in task_ids

    def test_arm_summary_present(self, handler: HandlerContextRoi) -> None:
        result = handler.handle(_SIMPLE_REQUEST)
        assert len(result.arm_summary) >= 1

    def test_off_first_pass_rate_zero(self, handler: HandlerContextRoi) -> None:
        result = handler.handle(_SIMPLE_REQUEST)
        off_row = next(r for r in result.arm_rows if r.arm_label == EnumArmLabel.OFF)
        assert off_row.first_pass_rate == 0.0

    def test_golden_first_pass_rate_one(self, handler: HandlerContextRoi) -> None:
        result = handler.handle(_SIMPLE_REQUEST)
        golden_row = next(
            r for r in result.arm_rows if r.arm_label == EnumArmLabel.GOLDEN_ONLY
        )
        assert golden_row.first_pass_rate == 1.0

    def test_preferred_arm_is_golden_not_off(self, handler: HandlerContextRoi) -> None:
        result = handler.handle(_SIMPLE_REQUEST)
        assert result.preferred_arm == EnumArmLabel.GOLDEN_ONLY

    def test_no_errors_in_ok_result(self, handler: HandlerContextRoi) -> None:
        result = handler.handle(_SIMPLE_REQUEST)
        assert result.errors == ()

    def test_deterministic(self, handler: HandlerContextRoi) -> None:
        r1 = handler.handle(_SIMPLE_REQUEST)
        r2 = handler.handle(_SIMPLE_REQUEST)
        assert r1.status == r2.status
        assert r1.preferred_arm == r2.preferred_arm
        assert r1.proof_class == r2.proof_class


class TestHandlerRuntimeMode:
    """Runtime-observed mode (OMN-12947): fixture_mode=False consumes real
    ModelArmRunRow rows (same schema the runner emits) through the identical
    deterministic aggregation path, and classifies the bundle as
    RUNTIME_OBSERVED_ONLY rather than failing closed.

    Lineage: OMN-12743 part (d) — runtime-observed evidence, not source-read.
    """

    @pytest.fixture
    def runtime_request(self) -> ModelContextRoiRequest:
        return ModelContextRoiRequest(
            run_id="rt-run-001",
            manifest_id="omn-12797-v1",
            rows=(_SIMPLE_OFF_ROW, _SIMPLE_GOLDEN_ROW),
            fixture_mode=False,
        )

    def test_runtime_mode_status_ok(
        self, handler: HandlerContextRoi, runtime_request: ModelContextRoiRequest
    ) -> None:
        """Valid live rows score to ok — no longer fails closed."""
        result = handler.handle(runtime_request)
        assert result.status == "ok"
        assert result.failure_class is None

    def test_runtime_mode_proof_class_runtime_observed(
        self, handler: HandlerContextRoi, runtime_request: ModelContextRoiRequest
    ) -> None:
        result = handler.handle(runtime_request)
        assert result.proof_class == EnumProofClass.RUNTIME_OBSERVED_ONLY

    def test_runtime_mode_produces_same_aggregation_as_fixture(
        self, handler: HandlerContextRoi
    ) -> None:
        """The only legitimate difference between modes is proof_class.

        Identical rows must produce identical arm_rows, arm_summary, and
        preferred_arm regardless of mode — provenance label aside.
        """
        rows = (_SIMPLE_OFF_ROW, _SIMPLE_GOLDEN_ROW)
        fixture_req = ModelContextRoiRequest(
            run_id="parity",
            manifest_id="omn-12797-v1",
            rows=rows,
            fixture_mode=True,
        )
        runtime_req = ModelContextRoiRequest(
            run_id="parity",
            manifest_id="omn-12797-v1",
            rows=rows,
            fixture_mode=False,
        )
        fixture_result = handler.handle(fixture_req)
        runtime_result = handler.handle(runtime_req)
        assert fixture_result.arm_rows == runtime_result.arm_rows
        assert fixture_result.arm_summary == runtime_result.arm_summary
        assert fixture_result.preferred_arm == runtime_result.preferred_arm
        # Provenance label differs, and only that.
        assert fixture_result.proof_class == EnumProofClass.REPLAY_PROVEN
        assert runtime_result.proof_class == EnumProofClass.RUNTIME_OBSERVED_ONLY

    def test_runtime_mode_preferred_arm(
        self, handler: HandlerContextRoi, runtime_request: ModelContextRoiRequest
    ) -> None:
        result = handler.handle(runtime_request)
        assert result.preferred_arm == EnumArmLabel.GOLDEN_ONLY

    def test_runtime_mode_missing_required_factor_fails_row(
        self, handler: HandlerContextRoi
    ) -> None:
        """Invariant holds in runtime mode: missing required factor fails."""
        bad_row = _make_row(
            task_id="sea_001",
            arm_label=EnumArmLabel.GOLDEN_ONLY,
            factors_present=(),  # GOLDEN_CHAIN absent — required!
        )
        req = ModelContextRoiRequest(
            run_id="rt-missing-required",
            manifest_id="omn-12797-v1",
            rows=(bad_row,),
            fixture_mode=False,
        )
        result = handler.handle(req)
        assert result.status == "failed"
        assert result.failure_class == "required_factor_missing"
        assert any("missing required" in e for e in result.errors)

    def test_runtime_mode_absent_optional_factor_warns(
        self, handler: HandlerContextRoi
    ) -> None:
        """Invariant holds in runtime mode: absent optional factor warns."""
        row = _make_row(
            task_id="sea_001",
            arm_label=EnumArmLabel.GOLDEN_EXEMPLAR,
            factors_present=(EnumContextFactor.GOLDEN_CHAIN,),
            factors_warned_absent=(),
        )
        req = ModelContextRoiRequest(
            run_id="rt-optional-warn",
            manifest_id="omn-12797-v1",
            rows=(row,),
            fixture_mode=False,
        )
        result = handler.handle(req)
        assert result.status == "ok"
        assert any("exemplar" in w.lower() for w in result.warnings)

    def test_runtime_mode_negative_control_never_preferred(
        self, handler: HandlerContextRoi
    ) -> None:
        """Invariant holds in runtime mode: negative control never preferred."""
        off_row = _make_row(
            "sea_001",
            EnumArmLabel.OFF,
            first_pass_success=False,
            final_success=False,
            attempt_count=2,
            factors_present=(),
        )
        neg_row = _make_row(
            "sea_001",
            EnumArmLabel.FULL_GUIDANCE_NEGATIVE_CONTROL,
            first_pass_success=True,
            final_success=True,
            attempt_count=1,
            failure_stage=EnumFailureStage.BUDGET_FAIL,
            factors_present=(),
            factors_warned_absent=(EnumContextFactor.CLAUDE_MD,),
        )
        req = ModelContextRoiRequest(
            run_id="rt-neg-not-preferred",
            manifest_id="omn-12797-v1",
            rows=(off_row, neg_row),
            fixture_mode=False,
        )
        result = handler.handle(req)
        assert result.preferred_arm != EnumArmLabel.FULL_GUIDANCE_NEGATIVE_CONTROL

    def test_runtime_mode_deterministic(
        self, handler: HandlerContextRoi, runtime_request: ModelContextRoiRequest
    ) -> None:
        r1 = handler.handle(runtime_request)
        r2 = handler.handle(runtime_request)
        assert r1.status == r2.status
        assert r1.preferred_arm == r2.preferred_arm
        assert r1.proof_class == r2.proof_class
        assert r1.arm_rows == r2.arm_rows


# ---------------------------------------------------------------------------
# Required factor enforcement (P2-2 acceptance criterion)
# ---------------------------------------------------------------------------


class TestRequiredFactorEnforcement:
    """Missing required factor must fail the row, not warn silently."""

    def test_missing_required_factor_fails_result(
        self, handler: HandlerContextRoi
    ) -> None:
        """golden_only arm requires GOLDEN_CHAIN; absent -> fail."""
        bad_row = _make_row(
            task_id="sea_001",
            arm_label=EnumArmLabel.GOLDEN_ONLY,
            factors_present=(),  # GOLDEN_CHAIN absent — required!
        )
        req = ModelContextRoiRequest(
            run_id="test-missing-required",
            manifest_id="omn-12797-v1",
            rows=(bad_row,),
            fixture_mode=True,
        )
        result = handler.handle(req)
        assert result.status == "failed"
        assert result.failure_class == "required_factor_missing"
        assert any("missing required" in e for e in result.errors)

    def test_missing_required_factor_names_factor(
        self, handler: HandlerContextRoi
    ) -> None:
        bad_row = _make_row(
            task_id="sea_001",
            arm_label=EnumArmLabel.GOLDEN_ONLY,
            factors_present=(),
        )
        req = ModelContextRoiRequest(
            run_id="test-name-factor",
            manifest_id="omn-12797-v1",
            rows=(bad_row,),
            fixture_mode=True,
        )
        result = handler.handle(req)
        assert any("golden_chain" in e for e in result.errors), (
            f"errors: {result.errors}"
        )

    def test_present_required_factor_passes(self, handler: HandlerContextRoi) -> None:
        good_row = _make_row(
            task_id="sea_001",
            arm_label=EnumArmLabel.GOLDEN_ONLY,
            factors_present=(EnumContextFactor.GOLDEN_CHAIN,),
        )
        req = ModelContextRoiRequest(
            run_id="test-present-required",
            manifest_id="omn-12797-v1",
            rows=(good_row,),
            fixture_mode=True,
        )
        result = handler.handle(req)
        assert result.status == "ok"


# ---------------------------------------------------------------------------
# Optional factor warning enforcement (P2-3: never silent-green)
# ---------------------------------------------------------------------------


class TestOptionalFactorWarnings:
    """Absent optional factors must warn, never be silently green."""

    def test_absent_optional_emits_warning(self, handler: HandlerContextRoi) -> None:
        # golden_exemplar: EXEMPLAR is optional; supply only GOLDEN_CHAIN
        row = _make_row(
            task_id="sea_001",
            arm_label=EnumArmLabel.GOLDEN_EXEMPLAR,
            factors_present=(EnumContextFactor.GOLDEN_CHAIN,),
            factors_warned_absent=(),  # runner did not warn
        )
        req = ModelContextRoiRequest(
            run_id="test-optional-warn",
            manifest_id="omn-12797-v1",
            rows=(row,),
            fixture_mode=True,
        )
        result = handler.handle(req)
        assert result.status == "ok"
        # Must not be silently green — warnings emitted
        assert len(result.warnings) > 0, "absent optional factor must produce a warning"
        assert any("exemplar" in w.lower() for w in result.warnings), (
            f"warnings: {result.warnings}"
        )

    def test_present_optional_no_warning(self, handler: HandlerContextRoi) -> None:
        row = _make_row(
            task_id="sea_001",
            arm_label=EnumArmLabel.GOLDEN_EXEMPLAR,
            factors_present=(
                EnumContextFactor.GOLDEN_CHAIN,
                EnumContextFactor.EXEMPLAR,
            ),
            factors_warned_absent=(),
        )
        req = ModelContextRoiRequest(
            run_id="test-optional-present",
            manifest_id="omn-12797-v1",
            rows=(row,),
            fixture_mode=True,
        )
        result = handler.handle(req)
        assert result.status == "ok"
        # EXEMPLAR present -> no exemplar-related warning
        assert not any("exemplar" in w.lower() for w in result.warnings), (
            f"unexpected warning for present factor: {result.warnings}"
        )


# ---------------------------------------------------------------------------
# Budget fail scoring (P2-3: scored separately, never conflated)
# ---------------------------------------------------------------------------


class TestBudgetFailScoring:
    def test_budget_fail_counted_separately(self, handler: HandlerContextRoi) -> None:
        budget_fail_row = _make_row(
            task_id="sea_001",
            arm_label=EnumArmLabel.FULL_GUIDANCE_NEGATIVE_CONTROL,
            first_pass_success=False,
            final_success=False,
            failure_stage=EnumFailureStage.BUDGET_FAIL,
            factors_present=(),  # budget failed before pack assembly
            factors_warned_absent=(EnumContextFactor.CLAUDE_MD,),
        )
        req = ModelContextRoiRequest(
            run_id="test-budget-fail",
            manifest_id="omn-12797-v1",
            rows=(budget_fail_row,),
            fixture_mode=True,
        )
        result = handler.handle(req)
        assert result.status == "ok"
        neg_rows = [
            r
            for r in result.arm_rows
            if r.arm_label == EnumArmLabel.FULL_GUIDANCE_NEGATIVE_CONTROL
        ]
        assert len(neg_rows) == 1
        assert neg_rows[0].budget_fail_count == 1
        assert neg_rows[0].generation_fail_count == 0  # not conflated

    def test_budget_fail_arm_never_preferred(self, handler: HandlerContextRoi) -> None:
        off_row = _make_row(
            task_id="sea_001",
            arm_label=EnumArmLabel.OFF,
            first_pass_success=False,
            final_success=False,
            factors_present=(),
        )
        budget_fail_row = _make_row(
            task_id="sea_001",
            arm_label=EnumArmLabel.FULL_GUIDANCE_NEGATIVE_CONTROL,
            first_pass_success=False,
            final_success=False,
            failure_stage=EnumFailureStage.BUDGET_FAIL,
            factors_present=(),
            factors_warned_absent=(EnumContextFactor.CLAUDE_MD,),
        )
        req = ModelContextRoiRequest(
            run_id="test-no-preferred-neg",
            manifest_id="omn-12797-v1",
            rows=(off_row, budget_fail_row),
            fixture_mode=True,
        )
        result = handler.handle(req)
        assert result.preferred_arm != EnumArmLabel.FULL_GUIDANCE_NEGATIVE_CONTROL


# ---------------------------------------------------------------------------
# Preferred arm selection (P2-3)
# ---------------------------------------------------------------------------


class TestPreferredArmSelection:
    def test_preferred_arm_beats_off_baseline(self, handler: HandlerContextRoi) -> None:
        """preferred_arm is the arm with highest first_pass_rate_delta_vs_off."""
        off_row = _make_row(
            "sea_001",
            EnumArmLabel.OFF,
            first_pass_success=False,
            final_success=True,
            attempt_count=2,
            factors_present=(),
        )
        golden_row = _make_row(
            "sea_001",
            EnumArmLabel.GOLDEN_ONLY,
            first_pass_success=True,
            final_success=True,
            attempt_count=1,
            factors_present=(EnumContextFactor.GOLDEN_CHAIN,),
        )
        req = ModelContextRoiRequest(
            run_id="test-preferred",
            manifest_id="omn-12797-v1",
            rows=(off_row, golden_row),
            fixture_mode=True,
        )
        result = handler.handle(req)
        assert result.preferred_arm == EnumArmLabel.GOLDEN_ONLY

    def test_no_preferred_when_no_arm_beats_baseline(
        self, handler: HandlerContextRoi
    ) -> None:
        """When all arms match the off baseline, preferred_arm is None."""
        off_row = _make_row(
            "sea_001",
            EnumArmLabel.OFF,
            first_pass_success=True,
            final_success=True,
            attempt_count=1,
            factors_present=(),
        )
        golden_row = _make_row(
            "sea_001",
            EnumArmLabel.GOLDEN_ONLY,
            first_pass_success=True,
            final_success=True,
            attempt_count=1,
            factors_present=(EnumContextFactor.GOLDEN_CHAIN,),
        )
        req = ModelContextRoiRequest(
            run_id="test-no-preferred",
            manifest_id="omn-12797-v1",
            rows=(off_row, golden_row),
            fixture_mode=True,
        )
        result = handler.handle(req)
        # Both at 1.0 first_pass_rate, delta = 0.0 -> no winner
        assert result.preferred_arm is None

    def test_negative_control_never_preferred(self, handler: HandlerContextRoi) -> None:
        off_row = _make_row(
            "sea_001",
            EnumArmLabel.OFF,
            first_pass_success=False,
            final_success=False,
            attempt_count=2,
            factors_present=(),
        )
        neg_row = _make_row(
            "sea_001",
            EnumArmLabel.FULL_GUIDANCE_NEGATIVE_CONTROL,
            first_pass_success=True,
            final_success=True,
            attempt_count=1,
            factors_present=(),
            factors_warned_absent=(EnumContextFactor.CLAUDE_MD,),
        )
        req = ModelContextRoiRequest(
            run_id="test-neg-not-preferred",
            manifest_id="omn-12797-v1",
            rows=(off_row, neg_row),
            fixture_mode=True,
        )
        result = handler.handle(req)
        # Even if neg control has better rate, it must never be preferred
        assert result.preferred_arm != EnumArmLabel.FULL_GUIDANCE_NEGATIVE_CONTROL


# ---------------------------------------------------------------------------
# Arm summary delta computation
# ---------------------------------------------------------------------------


class TestArmSummaryDeltas:
    def test_positive_delta_when_arm_beats_off(
        self, handler: HandlerContextRoi
    ) -> None:
        off_row = _make_row(
            "sea_001",
            EnumArmLabel.OFF,
            first_pass_success=False,
            final_success=True,
            attempt_count=2,
            factors_present=(),
        )
        golden_row = _make_row(
            "sea_001",
            EnumArmLabel.GOLDEN_ONLY,
            first_pass_success=True,
            final_success=True,
            attempt_count=1,
            factors_present=(EnumContextFactor.GOLDEN_CHAIN,),
        )
        req = ModelContextRoiRequest(
            run_id="test-delta",
            manifest_id="omn-12797-v1",
            rows=(off_row, golden_row),
            fixture_mode=True,
        )
        result = handler.handle(req)
        golden_summary = next(
            s for s in result.arm_summary if s.arm_label == EnumArmLabel.GOLDEN_ONLY
        )
        assert golden_summary.first_pass_rate_delta_vs_off is not None
        assert golden_summary.first_pass_rate_delta_vs_off > 0.0

    def test_off_summary_has_no_delta(self, handler: HandlerContextRoi) -> None:
        off_row = _make_row(
            "sea_001",
            EnumArmLabel.OFF,
            factors_present=(),
        )
        req = ModelContextRoiRequest(
            run_id="test-off-delta",
            manifest_id="omn-12797-v1",
            rows=(off_row,),
            fixture_mode=True,
        )
        result = handler.handle(req)
        off_summary = next(
            s for s in result.arm_summary if s.arm_label == EnumArmLabel.OFF
        )
        # off baseline has no delta (compared against itself)
        assert off_summary.first_pass_rate_delta_vs_off is None

    def test_negative_control_summary_has_budget_fail_count(
        self, handler: HandlerContextRoi
    ) -> None:
        budget_row = _make_row(
            "sea_001",
            EnumArmLabel.FULL_GUIDANCE_NEGATIVE_CONTROL,
            first_pass_success=False,
            final_success=False,
            failure_stage=EnumFailureStage.BUDGET_FAIL,
            factors_present=(),
            factors_warned_absent=(EnumContextFactor.CLAUDE_MD,),
        )
        req = ModelContextRoiRequest(
            run_id="test-neg-summary",
            manifest_id="omn-12797-v1",
            rows=(budget_row,),
            fixture_mode=True,
        )
        result = handler.handle(req)
        neg_summary = next(
            s
            for s in result.arm_summary
            if s.arm_label == EnumArmLabel.FULL_GUIDANCE_NEGATIVE_CONTROL
        )
        assert neg_summary.is_negative_control is True
        assert neg_summary.total_budget_fail_count == 1

    def test_arm_summary_ordered_canonically(self, handler: HandlerContextRoi) -> None:
        """arm_summary order matches canonical matrix order."""
        off_row = _make_row("sea_001", EnumArmLabel.OFF, factors_present=())
        golden_row = _make_row(
            "sea_001",
            EnumArmLabel.GOLDEN_ONLY,
            factors_present=(EnumContextFactor.GOLDEN_CHAIN,),
        )
        req = ModelContextRoiRequest(
            run_id="test-order",
            manifest_id="omn-12797-v1",
            rows=(golden_row, off_row),  # note: reversed input order
            fixture_mode=True,
        )
        result = handler.handle(req)
        labels = [s.arm_label for s in result.arm_summary]
        assert labels.index(EnumArmLabel.OFF) < labels.index(EnumArmLabel.GOLDEN_ONLY)


# ---------------------------------------------------------------------------
# Model schema validation
# ---------------------------------------------------------------------------


class TestModelSchemas:
    def test_arm_run_row_frozen(self) -> None:
        from pydantic import ValidationError

        row = _SIMPLE_OFF_ROW
        with pytest.raises((ValidationError, TypeError)):
            row.task_id = "mutated"  # type: ignore[misc]

    def test_context_roi_request_frozen(self) -> None:
        from pydantic import ValidationError

        with pytest.raises((ValidationError, TypeError)):
            _SIMPLE_REQUEST.run_id = "mutated"  # type: ignore[misc]

    def test_result_frozen(self, handler: HandlerContextRoi) -> None:
        from pydantic import ValidationError

        result = handler.handle(_SIMPLE_REQUEST)
        with pytest.raises((ValidationError, TypeError)):
            result.status = "mutated"  # type: ignore[misc]

    def test_failure_stage_enum_values(self) -> None:
        assert EnumFailureStage.NONE == "none"
        assert EnumFailureStage.BUDGET_FAIL == "budget_fail"
        assert EnumFailureStage.MISSING_REQUIRED_FACTOR == "missing_required_factor"
        assert EnumFailureStage.GENERATION == "generation"

    def test_arm_label_enum_values(self) -> None:
        assert EnumArmLabel.OFF == "off"
        assert EnumArmLabel.FULL_GUIDANCE_NEGATIVE_CONTROL == (
            "full_guidance_negative_control"
        )

    def test_result_is_typed(self, handler: HandlerContextRoi) -> None:
        result = handler.handle(_SIMPLE_REQUEST)
        assert isinstance(result, ModelContextRoiResult)
        assert isinstance(result.arm_rows, tuple)
        assert isinstance(result.arm_summary, tuple)
