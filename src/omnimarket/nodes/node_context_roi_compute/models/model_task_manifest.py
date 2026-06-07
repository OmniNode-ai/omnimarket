# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixed task manifest for the context-ROI experiment (OMN-12797 P2-2).

The manifest declares:
  - A bounded, replayable, representative set of SEA-path tasks
  - Per-task success criteria (explicit, not implicit)
  - Per-arm required/optional factor declarations so malformed rows
    cannot be silently hidden

Task coverage (representative of the SEA path per P2-2 spec):
  1. Node-contract correctness — generate a valid contract.yaml from spec
  2. Handler correctness — generate a handler that satisfies the contract
  3. Event-topic alignment — verify topic strings match across producer/consumer
  4. Projection/read-model expectations — assert projection fields match contract
  5. Local validation/test repair — fix a failing unit test from its error output

Each task carries:
  - task_id: stable, never reused identifier
  - description: human-readable task description
  - sea_path_category: which SEA-path concern the task exercises
  - success_criteria: explicit, ordered list of pass conditions
  - per-arm required/optional factor declarations (via ModelTaskArmPolicy)
    a required factor absent in the resolved artifacts MUST fail the row,
    not warn. No result may depend on hidden session memory.
"""

from __future__ import annotations

from enum import StrEnum

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_context_roi_compute.models.model_factor_arm import (
    EnumArmLabel,
)


class EnumSeaPathCategory(StrEnum):
    """Which aspect of the SEA path a task exercises."""

    NODE_CONTRACT_CORRECTNESS = "node_contract_correctness"
    HANDLER_CORRECTNESS = "handler_correctness"
    EVENT_TOPIC_ALIGNMENT = "event_topic_alignment"
    PROJECTION_EXPECTATIONS = "projection_expectations"
    LOCAL_VALIDATION_TEST_REPAIR = "local_validation_test_repair"


class EnumFailureStage(StrEnum):
    """Explicit failure stage recorded on a task-arm row.

    none: the arm ran to completion without failure.
    missing_required_factor: a required factor was absent from resolved artifacts;
        the row fails immediately before pack assembly.
    pack_build: the pack builder rejected the assembled artifacts (e.g. dedup).
    budget_fail: the token budget was exceeded; the arm never reached generation.
        For full_guidance_negative_control this is an expected finding.
    generation: generation ran but produced no valid contract within max_attempts.
    validation: generation produced a contract but contract validation failed.
    downstream_gate: a downstream quality gate (e.g. pytest / is_healthy) failed.
    """

    NONE = "none"
    MISSING_REQUIRED_FACTOR = "missing_required_factor"
    PACK_BUILD = "pack_build"
    BUDGET_FAIL = "budget_fail"
    GENERATION = "generation"
    VALIDATION = "validation"
    DOWNSTREAM_GATE = "downstream_gate"


class ModelTaskSuccessCriterion(BaseModel):
    """One explicit, verifiable success condition for a task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion_id: str = Field(
        description="Stable identifier, e.g. 'contract_yaml_valid'"
    )
    description: str = Field(description="Human-readable pass condition")
    verification_method: str = Field(
        description=(
            "How to verify: 'contract_validation', 'schema_check', "
            "'topic_string_match', 'pytest_pass', 'field_presence_check'"
        )
    )


class ModelTaskArmPolicy(BaseModel):
    """Per-arm factor policy declaration for a specific task.

    required_factors: factors that MUST be present in the resolved artifacts
        for this arm on this task. Missing required factor -> fail the row,
        never warn silently.
    optional_factors: factors whose absence is tolerated but MUST be warned.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    arm_label: EnumArmLabel
    required_factors: tuple[EnumContextFactor, ...] = Field(default_factory=tuple)
    optional_factors: tuple[EnumContextFactor, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _required_optional_disjoint(self) -> ModelTaskArmPolicy:
        overlap = set(self.required_factors) & set(self.optional_factors)
        if overlap:
            raise ValueError(
                f"arm {self.arm_label}: required_factors and optional_factors "
                f"overlap: {', '.join(f.value for f in sorted(overlap, key=lambda x: x.value))}"
            )
        return self


class ModelExperimentTask(BaseModel):
    """A single task in the fixed experiment manifest.

    No result for this task may depend on hidden session memory — all context
    must come from declared context artifacts supplied by the runner.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(description="Stable identifier, never reused. E.g. 'sea_001'.")
    description: str = Field(description="Human-readable task description")
    sea_path_category: EnumSeaPathCategory = Field(
        description="Which SEA-path concern this task exercises"
    )
    task_prompt: str = Field(
        description=(
            "The exact prompt text sent to the generation pipeline. "
            "Must be self-contained; must not reference session memory."
        )
    )
    success_criteria: tuple[ModelTaskSuccessCriterion, ...] = Field(
        min_length=1,
        description="Explicit, ordered pass conditions. At least one required.",
    )
    arm_policies: tuple[ModelTaskArmPolicy, ...] = Field(
        default_factory=tuple,
        description=(
            "Per-arm required/optional factor overrides for this task. "
            "Arms not listed fall back to the arm-level defaults from ModelFactorArm."
        ),
    )

    def arm_policy(self, label: EnumArmLabel) -> ModelTaskArmPolicy | None:
        """Return the task-specific arm policy, or None to use arm-level defaults."""
        for policy in self.arm_policies:
            if policy.arm_label == label:
                return policy
        return None


class ModelTaskManifest(BaseModel):
    """Bounded, replayable, representative fixed task set (P2-2).

    This is the single source of truth for which tasks run in the experiment.
    Every task declares explicit success criteria and per-arm factor policies.
    The manifest is immutable once constructed; runners replay it unchanged.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_id: str = Field(
        description=(
            "Stable identifier for this manifest version, e.g. 'omn-12797-v1'. "
            "Bump when tasks or success criteria change."
        )
    )
    description: str = Field(
        description="Human-readable description of what this manifest covers"
    )
    tasks: tuple[ModelExperimentTask, ...] = Field(
        min_length=1,
        description="Ordered fixed task set. Order is stable across runs.",
    )

    @model_validator(mode="after")
    def _task_ids_unique(self) -> ModelTaskManifest:
        seen: set[str] = set()
        for task in self.tasks:
            if task.task_id in seen:
                raise ValueError(f"duplicate task_id in manifest: {task.task_id!r}")
            seen.add(task.task_id)
        return self

    def task_by_id(self, task_id: str) -> ModelExperimentTask:
        """Return the task with the given id, or raise KeyError."""
        for task in self.tasks:
            if task.task_id == task_id:
                return task
        raise KeyError(f"no task with id {task_id!r} in manifest {self.manifest_id!r}")


def build_canonical_task_manifest() -> ModelTaskManifest:
    """Construct the canonical fixed task manifest for OMN-12797.

    Tasks are representative of the full SEA path:
      - node-contract correctness
      - handler correctness
      - event-topic alignment
      - projection/read-model expectations
      - local validation/test repair

    Each task carries explicit success criteria and per-arm factor policies.
    No task description references session memory or external state.
    """
    return ModelTaskManifest(
        manifest_id="omn-12797-v1",
        description=(
            "Fixed context-ROI experiment task set (OMN-12797 P2-2). "
            "Covers all five SEA-path categories: contract correctness, "
            "handler correctness, topic alignment, projection expectations, "
            "and test repair. Replayable; no hidden session memory."
        ),
        tasks=(
            # ------------------------------------------------------------------
            # Task 1: Node-contract correctness
            # ------------------------------------------------------------------
            ModelExperimentTask(
                task_id="sea_001",
                description=(
                    "Generate a valid ONEX contract.yaml for a pure COMPUTE node "
                    "that accepts a list of integers, sums them, and emits a result "
                    "containing the sum and a count."
                ),
                sea_path_category=EnumSeaPathCategory.NODE_CONTRACT_CORRECTNESS,
                task_prompt=(
                    "Write a valid ONEX contract.yaml for a pure COMPUTE node named "
                    "'node_sum_compute'. The node accepts: a list of integers named "
                    "'values' (required) and an optional string 'label'. It produces: "
                    "'sum' (integer, required), 'count' (integer, required), and "
                    "'label' (string, optional). "
                    "Use node_archetype: compute, purity: pure, idempotent: true. "
                    "Declare subscribe/publish topics following the pattern "
                    "onex.{cmd|evt}.omnimarket.{kebab-action}.v1. "
                    "Output only the YAML content, no extra explanation."
                ),
                success_criteria=(
                    ModelTaskSuccessCriterion(
                        criterion_id="contract_yaml_parseable",
                        description="Generated output is valid YAML",
                        verification_method="schema_check",
                    ),
                    ModelTaskSuccessCriterion(
                        criterion_id="contract_node_archetype_compute",
                        description=(
                            "descriptor.node_archetype == 'compute' and "
                            "descriptor.purity == 'pure'"
                        ),
                        verification_method="contract_validation",
                    ),
                    ModelTaskSuccessCriterion(
                        criterion_id="contract_topics_follow_convention",
                        description=(
                            "subscribe_topics and publish_topics follow "
                            "onex.{cmd|evt}.omnimarket.{kebab-action}.v1 pattern"
                        ),
                        verification_method="topic_string_match",
                    ),
                    ModelTaskSuccessCriterion(
                        criterion_id="contract_declares_required_inputs",
                        description="inputs declares 'values' as required",
                        verification_method="field_presence_check",
                    ),
                ),
                arm_policies=(
                    # off arm: no context, no required factors
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.OFF,
                        required_factors=(),
                        optional_factors=(),
                    ),
                    # golden_only: golden chain required — contract patterns are there
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.GOLDEN_ONLY,
                        required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
                        optional_factors=(),
                    ),
                    # golden_exemplar: golden chain required, exemplar optional
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.GOLDEN_EXEMPLAR,
                        required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
                        optional_factors=(EnumContextFactor.EXEMPLAR,),
                    ),
                    # golden_exemplar_failures: golden required, exemplar+failures optional
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.GOLDEN_EXEMPLAR_FAILURES,
                        required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
                        optional_factors=(
                            EnumContextFactor.EXEMPLAR,
                            EnumContextFactor.LOCAL_FAILURES,
                        ),
                    ),
                    # structured_context: golden required, rest optional
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.STRUCTURED_CONTEXT,
                        required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
                        optional_factors=(
                            EnumContextFactor.EXEMPLAR,
                            EnumContextFactor.LOCAL_FAILURES,
                            EnumContextFactor.ARCHITECTURE_PATTERNS,
                        ),
                    ),
                    # structured_plus_guidance_chunks: golden required, rest optional
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.STRUCTURED_PLUS_GUIDANCE_CHUNKS,
                        required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
                        optional_factors=(
                            EnumContextFactor.EXEMPLAR,
                            EnumContextFactor.LOCAL_FAILURES,
                            EnumContextFactor.ARCHITECTURE_PATTERNS,
                            EnumContextFactor.CLAUDE_MD,
                        ),
                    ),
                    # full_guidance_negative_control: no required, claude_md optional
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.FULL_GUIDANCE_NEGATIVE_CONTROL,
                        required_factors=(),
                        optional_factors=(EnumContextFactor.CLAUDE_MD,),
                    ),
                ),
            ),
            # ------------------------------------------------------------------
            # Task 2: Handler correctness
            # ------------------------------------------------------------------
            ModelExperimentTask(
                task_id="sea_002",
                description=(
                    "Generate a Python handler class that implements the contract "
                    "for a COMPUTE node that validates a Pydantic model and "
                    "returns a status and error list."
                ),
                sea_path_category=EnumSeaPathCategory.HANDLER_CORRECTNESS,
                task_prompt=(
                    "Write a Python handler class named 'HandlerValidateModel' "
                    "for a pure COMPUTE node. The handler's handle() method accepts "
                    "a Pydantic BaseModel input with fields: 'data' (dict, required) "
                    "and 'schema_version' (str, required). "
                    "It returns a result model with: 'status' (str: 'ok' or 'failed'), "
                    "'errors' (tuple of str, default empty tuple). "
                    "Use 'from __future__ import annotations'. "
                    "All models must use ConfigDict(frozen=True, extra='forbid'). "
                    "PEP 604 unions only (X | Y, not Optional[X]). "
                    "Output only the Python code, no extra explanation."
                ),
                success_criteria=(
                    ModelTaskSuccessCriterion(
                        criterion_id="handler_class_present",
                        description="Output defines a class named 'HandlerValidateModel'",
                        verification_method="schema_check",
                    ),
                    ModelTaskSuccessCriterion(
                        criterion_id="handler_handle_method_present",
                        description=(
                            "Class has a handle() method with typed input/output"
                        ),
                        verification_method="contract_validation",
                    ),
                    ModelTaskSuccessCriterion(
                        criterion_id="handler_uses_frozen_config_dict",
                        description=(
                            "Models use ConfigDict(frozen=True, extra='forbid')"
                        ),
                        verification_method="schema_check",
                    ),
                    ModelTaskSuccessCriterion(
                        criterion_id="handler_no_optional_union",
                        description=("No Optional[X] or Union[X, Y] usage; uses X | Y"),
                        verification_method="schema_check",
                    ),
                ),
                arm_policies=(
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.OFF,
                        required_factors=(),
                        optional_factors=(),
                    ),
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.GOLDEN_ONLY,
                        required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
                        optional_factors=(),
                    ),
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.GOLDEN_EXEMPLAR,
                        required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
                        optional_factors=(EnumContextFactor.EXEMPLAR,),
                    ),
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.GOLDEN_EXEMPLAR_FAILURES,
                        required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
                        optional_factors=(
                            EnumContextFactor.EXEMPLAR,
                            EnumContextFactor.LOCAL_FAILURES,
                        ),
                    ),
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.STRUCTURED_CONTEXT,
                        required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
                        optional_factors=(
                            EnumContextFactor.EXEMPLAR,
                            EnumContextFactor.LOCAL_FAILURES,
                            EnumContextFactor.ARCHITECTURE_PATTERNS,
                        ),
                    ),
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.STRUCTURED_PLUS_GUIDANCE_CHUNKS,
                        required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
                        optional_factors=(
                            EnumContextFactor.EXEMPLAR,
                            EnumContextFactor.LOCAL_FAILURES,
                            EnumContextFactor.ARCHITECTURE_PATTERNS,
                            EnumContextFactor.CLAUDE_MD,
                        ),
                    ),
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.FULL_GUIDANCE_NEGATIVE_CONTROL,
                        required_factors=(),
                        optional_factors=(EnumContextFactor.CLAUDE_MD,),
                    ),
                ),
            ),
            # ------------------------------------------------------------------
            # Task 3: Event-topic alignment
            # ------------------------------------------------------------------
            ModelExperimentTask(
                task_id="sea_003",
                description=(
                    "Generate a contract.yaml where the subscribe and publish "
                    "topic strings exactly follow the ONEX naming convention and "
                    "are byte-identical to what a consumer would declare."
                ),
                sea_path_category=EnumSeaPathCategory.EVENT_TOPIC_ALIGNMENT,
                task_prompt=(
                    "Write a minimal contract.yaml for an EFFECT node named "
                    "'node_alert_sender_effect' that: "
                    "- subscribes to a command topic for sending alerts "
                    "- publishes a completed event and a failed event "
                    "All topic strings must follow: "
                    "onex.{cmd|evt}.omnimarket.{kebab-action}.v1 "
                    "Commands use 'cmd', events use 'evt'. "
                    "The action part should be 'alert-send'. "
                    "Set node_archetype: effect. "
                    "Output only the YAML content."
                ),
                success_criteria=(
                    ModelTaskSuccessCriterion(
                        criterion_id="subscribe_topic_cmd_prefix",
                        description="subscribe_topics use the cmd namespace",
                        verification_method="topic_string_match",
                    ),
                    ModelTaskSuccessCriterion(
                        criterion_id="publish_topics_evt_prefix",
                        description="publish_topics use the evt namespace",
                        verification_method="topic_string_match",
                    ),
                    ModelTaskSuccessCriterion(
                        criterion_id="topic_versioned_v1",
                        description="All topics end in '.v1'",
                        verification_method="topic_string_match",
                    ),
                    ModelTaskSuccessCriterion(
                        criterion_id="completed_and_failed_events_declared",
                        description=(
                            "publish_topics declares both a '-completed.v1' "
                            "and a '-failed.v1' event"
                        ),
                        verification_method="field_presence_check",
                    ),
                ),
                arm_policies=(
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.OFF,
                        required_factors=(),
                        optional_factors=(),
                    ),
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.GOLDEN_ONLY,
                        required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
                        optional_factors=(),
                    ),
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.GOLDEN_EXEMPLAR,
                        required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
                        optional_factors=(EnumContextFactor.EXEMPLAR,),
                    ),
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.GOLDEN_EXEMPLAR_FAILURES,
                        required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
                        optional_factors=(
                            EnumContextFactor.EXEMPLAR,
                            EnumContextFactor.LOCAL_FAILURES,
                        ),
                    ),
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.STRUCTURED_CONTEXT,
                        required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
                        optional_factors=(
                            EnumContextFactor.EXEMPLAR,
                            EnumContextFactor.LOCAL_FAILURES,
                            EnumContextFactor.ARCHITECTURE_PATTERNS,
                        ),
                    ),
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.STRUCTURED_PLUS_GUIDANCE_CHUNKS,
                        required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
                        optional_factors=(
                            EnumContextFactor.EXEMPLAR,
                            EnumContextFactor.LOCAL_FAILURES,
                            EnumContextFactor.ARCHITECTURE_PATTERNS,
                            EnumContextFactor.CLAUDE_MD,
                        ),
                    ),
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.FULL_GUIDANCE_NEGATIVE_CONTROL,
                        required_factors=(),
                        optional_factors=(EnumContextFactor.CLAUDE_MD,),
                    ),
                ),
            ),
            # ------------------------------------------------------------------
            # Task 4: Projection/read-model expectations
            # ------------------------------------------------------------------
            ModelExperimentTask(
                task_id="sea_004",
                description=(
                    "Generate a Pydantic read-model (projection) for the output "
                    "of a generation event, ensuring all required fields from "
                    "the event contract are present and typed correctly."
                ),
                sea_path_category=EnumSeaPathCategory.PROJECTION_EXPECTATIONS,
                task_prompt=(
                    "Write a Python Pydantic model named 'ModelGenerationProjection' "
                    "that represents a read-model projection for a generation-completed event. "
                    "The model must include all of these fields: "
                    "'correlation_id' (str, required), "
                    "'contract_yaml' (str, required), "
                    "'handler_source' (str, required), "
                    "'attempt_count' (int, required, >= 1), "
                    "'contract_passed' (bool, required), "
                    "'model_id' (str, required), "
                    "'cost_inference_usd' (float, required, >= 0.0), "
                    "'total_latency_e2e_ms' (float | None, default None). "
                    "Use ConfigDict(frozen=True, extra='ignore') for projection models "
                    "(they receive external events and must tolerate extra fields). "
                    "Use PEP 604 unions. "
                    "Output only the Python code."
                ),
                success_criteria=(
                    ModelTaskSuccessCriterion(
                        criterion_id="projection_class_present",
                        description=("Output defines 'ModelGenerationProjection'"),
                        verification_method="schema_check",
                    ),
                    ModelTaskSuccessCriterion(
                        criterion_id="projection_all_required_fields",
                        description=(
                            "Model includes all required fields: "
                            "correlation_id, contract_yaml, handler_source, "
                            "attempt_count, contract_passed, model_id, "
                            "cost_inference_usd"
                        ),
                        verification_method="field_presence_check",
                    ),
                    ModelTaskSuccessCriterion(
                        criterion_id="projection_extra_ignore",
                        description=(
                            "Uses extra='ignore' in ConfigDict "
                            "(projection models receive external events)"
                        ),
                        verification_method="schema_check",
                    ),
                    ModelTaskSuccessCriterion(
                        criterion_id="projection_attempt_count_ge_1",
                        description="attempt_count has ge=1 constraint",
                        verification_method="contract_validation",
                    ),
                ),
                arm_policies=(
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.OFF,
                        required_factors=(),
                        optional_factors=(),
                    ),
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.GOLDEN_ONLY,
                        required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
                        optional_factors=(),
                    ),
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.GOLDEN_EXEMPLAR,
                        required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
                        optional_factors=(EnumContextFactor.EXEMPLAR,),
                    ),
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.GOLDEN_EXEMPLAR_FAILURES,
                        required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
                        optional_factors=(
                            EnumContextFactor.EXEMPLAR,
                            EnumContextFactor.LOCAL_FAILURES,
                        ),
                    ),
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.STRUCTURED_CONTEXT,
                        required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
                        optional_factors=(
                            EnumContextFactor.EXEMPLAR,
                            EnumContextFactor.LOCAL_FAILURES,
                            EnumContextFactor.ARCHITECTURE_PATTERNS,
                        ),
                    ),
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.STRUCTURED_PLUS_GUIDANCE_CHUNKS,
                        required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
                        optional_factors=(
                            EnumContextFactor.EXEMPLAR,
                            EnumContextFactor.LOCAL_FAILURES,
                            EnumContextFactor.ARCHITECTURE_PATTERNS,
                            EnumContextFactor.CLAUDE_MD,
                        ),
                    ),
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.FULL_GUIDANCE_NEGATIVE_CONTROL,
                        required_factors=(),
                        optional_factors=(EnumContextFactor.CLAUDE_MD,),
                    ),
                ),
            ),
            # ------------------------------------------------------------------
            # Task 5: Local validation/test repair
            # ------------------------------------------------------------------
            ModelExperimentTask(
                task_id="sea_005",
                description=(
                    "Given a failing pytest output, generate the minimal fix to "
                    "make the test pass without changing the test itself."
                ),
                sea_path_category=EnumSeaPathCategory.LOCAL_VALIDATION_TEST_REPAIR,
                task_prompt=(
                    "The following pytest output shows a failing test:\n\n"
                    "FAILED tests/test_sum_compute.py::TestSumCompute::test_sum_correct\n"
                    "  AssertionError: assert result.sum == 15\n"
                    "  where result.sum = 0\n\n"
                    "The test calls: handler.handle(ModelSumRequest(values=(1,2,3,4,5)))\n"
                    "and asserts result.sum == 15 and result.count == 5.\n\n"
                    "The handler's handle() method currently returns:\n"
                    "  return ModelSumResult(status='ok', sum=0, count=0)\n\n"
                    "Write the corrected handle() method body only. "
                    "Use sum(request.values) and len(request.values). "
                    "Output only the corrected method code."
                ),
                success_criteria=(
                    ModelTaskSuccessCriterion(
                        criterion_id="fix_uses_sum_builtin",
                        description=("Fix uses sum(request.values) or equivalent"),
                        verification_method="schema_check",
                    ),
                    ModelTaskSuccessCriterion(
                        criterion_id="fix_uses_len_for_count",
                        description=(
                            "Fix uses len(request.values) or equivalent for count"
                        ),
                        verification_method="schema_check",
                    ),
                    ModelTaskSuccessCriterion(
                        criterion_id="fix_returns_model_sum_result",
                        description=(
                            "Fix returns ModelSumResult with correct sum and count"
                        ),
                        verification_method="contract_validation",
                    ),
                    ModelTaskSuccessCriterion(
                        criterion_id="fix_does_not_modify_test",
                        description="Fix modifies only the handler, not the test",
                        verification_method="schema_check",
                    ),
                ),
                arm_policies=(
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.OFF,
                        required_factors=(),
                        optional_factors=(),
                    ),
                    # For test repair, LOCAL_FAILURES context is required
                    # (the error output is the key signal for this arm)
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.GOLDEN_ONLY,
                        required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
                        optional_factors=(),
                    ),
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.GOLDEN_EXEMPLAR,
                        required_factors=(EnumContextFactor.GOLDEN_CHAIN,),
                        optional_factors=(EnumContextFactor.EXEMPLAR,),
                    ),
                    # Local failures arm makes LOCAL_FAILURES required for repair tasks
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.GOLDEN_EXEMPLAR_FAILURES,
                        required_factors=(
                            EnumContextFactor.GOLDEN_CHAIN,
                            EnumContextFactor.LOCAL_FAILURES,
                        ),
                        optional_factors=(EnumContextFactor.EXEMPLAR,),
                    ),
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.STRUCTURED_CONTEXT,
                        required_factors=(
                            EnumContextFactor.GOLDEN_CHAIN,
                            EnumContextFactor.LOCAL_FAILURES,
                        ),
                        optional_factors=(
                            EnumContextFactor.EXEMPLAR,
                            EnumContextFactor.ARCHITECTURE_PATTERNS,
                        ),
                    ),
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.STRUCTURED_PLUS_GUIDANCE_CHUNKS,
                        required_factors=(
                            EnumContextFactor.GOLDEN_CHAIN,
                            EnumContextFactor.LOCAL_FAILURES,
                        ),
                        optional_factors=(
                            EnumContextFactor.EXEMPLAR,
                            EnumContextFactor.ARCHITECTURE_PATTERNS,
                            EnumContextFactor.CLAUDE_MD,
                        ),
                    ),
                    ModelTaskArmPolicy(
                        arm_label=EnumArmLabel.FULL_GUIDANCE_NEGATIVE_CONTROL,
                        required_factors=(),
                        optional_factors=(EnumContextFactor.CLAUDE_MD,),
                    ),
                ),
            ),
        ),
    )


__all__ = [
    "EnumFailureStage",
    "EnumSeaPathCategory",
    "ModelExperimentTask",
    "ModelTaskArmPolicy",
    "ModelTaskManifest",
    "ModelTaskSuccessCriterion",
    "build_canonical_task_manifest",
]
