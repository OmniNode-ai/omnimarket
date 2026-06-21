# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Generation pipeline models for node_generation_consumer.

OMN-12794 (P2-1): additive extensions for context-pack injection and
attempt-reduction telemetry.  All new fields are OPTIONAL with safe defaults
so existing producers and consumers are unaffected.  The emitter
(HandlerGenerationConsumer) is updated first; runner/scorer consumers follow.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_usage_source import EnumUsageSource

__all__ = [
    "EnumUsageSource",
    "ModelContextArtifact",
    "ModelCorpusFixture",
    "ModelGenerationAttempt",
    "ModelGenerationBenchmark",
    "ModelNodeDeploy",
    "ModelNodeGenerationRequest",
    "ModelValidatorCorpus",
]


class ModelCorpusFixture(BaseModel):
    """A single fixture in a validator-generation acceptance corpus (OMN-13289).

    The generated artifact under test is a *validator/scanner*: a ``handle``
    function that takes a source-text payload and returns findings. A fixture is
    one concrete source-text input plus the deterministic verdict the generated
    scanner MUST produce for it.

    The corpus is split into two named sets on ``ModelValidatorCorpus``:

    * ``violation_fixtures`` — the scanner MUST flag every one (>=1 finding).
    * ``clean_fixtures`` — the scanner MUST pass every one (0 findings).

    The naming is deliberate: ``violation``/``clean`` describe the *required
    verdict*, not ``positive``/``negative`` (which inverts trivially — §1B of the
    validator-standardization remediation plan forbids positive/negative naming).

    ``source`` is the raw input handed to the generated ``handle(input_data)`` as
    ``input_data[source_field]``. ``mutation_of`` marks an adversarial /
    planted-failure case derived by perturbing a base fixture id, so the corpus
    verdict cannot be passed by memorising a curated set (a gate that only passes
    hand-picked examples is not proven).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fixture_id: str = Field(
        description="Stable identifier for this fixture (for evidence + mutation provenance)"
    )
    source: str = Field(
        description=(
            "Raw source text handed to the generated scanner's handle(input_data) "
            "as input_data[source_field]. The unit the validator scans."
        )
    )
    description: str = Field(
        default="",
        description="Human-readable note on what this fixture probes",
    )
    mutation_of: str = Field(
        default="",
        description=(
            "fixture_id of the base fixture this case was derived from by "
            "adversarial perturbation. Empty for a hand-authored base fixture. "
            "A corpus with zero mutation cases is rejected: a gate that passes "
            "only curated examples is not proven (OMN-13289)."
        ),
    )


class ModelValidatorCorpus(BaseModel):
    """Fixture corpus that gates acceptance of a generated validator (OMN-13289).

    The corpus is the *acceptance authority* for a generated validator/scanner —
    NOT the LLM's self-report (memory ``feedback_adversarial_receipts``). A
    generated scanner is accepted iff it flags every ``violation_fixtures`` entry
    and produces zero findings on every ``clean_fixtures`` entry, evaluated by
    deterministic execution.

    ``source_field`` names the key under which each fixture's ``source`` is
    placed in the ``input_data`` mapping the generated ``handle`` receives.
    ``findings_keys`` are the acceptable output-field names under which the
    generated handler may return its findings list (a handler legitimately names
    it ``findings`` / ``violations`` / ``errors`` / ``matches``); a non-empty
    value under any one of them means "flagged".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_field: str = Field(
        default="source",
        min_length=1,
        description="Key in input_data carrying the fixture source the scanner reads",
    )
    findings_keys: tuple[str, ...] = Field(
        default=("findings", "violations", "errors", "matches"),
        description=(
            "Acceptable output-field names for the scanner's findings list. A "
            "non-empty value under any one means the input was flagged."
        ),
    )
    violation_fixtures: list[ModelCorpusFixture] = Field(
        default_factory=list,
        description="Inputs the generated scanner MUST flag (>=1 finding each)",
    )
    clean_fixtures: list[ModelCorpusFixture] = Field(
        default_factory=list,
        description="Inputs the generated scanner MUST pass (0 findings each)",
    )

    @property
    def is_empty(self) -> bool:
        """True when the corpus carries no fixtures at all (acceptance N/A)."""
        return not self.violation_fixtures and not self.clean_fixtures

    @property
    def has_mutation_case(self) -> bool:
        """True when at least one fixture is an adversarial mutation case.

        A corpus with no mutation cases is rejected at acceptance time: a gate
        that passes only hand-picked examples is not proven (OMN-13289 DoD).
        """
        return any(
            fx.mutation_of for fx in (*self.violation_fixtures, *self.clean_fixtures)
        )


class ModelContextArtifact(BaseModel):
    """A single context artifact injected into the generation prompt.

    Carries enough provenance for the runner to record which chunks were
    injected so the scorer can correlate context with outcome.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    factor: str = Field(
        description="Context factor label (matches EnumContextFactor values)"
    )
    content: str = Field(description="Raw text content of this artifact")
    source_ref: str = Field(
        default="",
        description="Source reference — file path, chain ID, or artifact key",
    )
    content_hash: str = Field(
        default="",
        description="SHA-256 hex digest of content (sha256:<hex>) for replay verification",
    )


class ModelGenerationAttempt(BaseModel):
    """A single generation attempt record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_number: int
    provider: str = ""
    model_id: str = ""
    endpoint_class: str = ""
    token_usage_input: int = 0
    token_usage_output: int = 0
    latency_inference_ms: int = 0
    contract_passed: bool = False
    # OMN-13166: behavioral pass, independent of contract/shape validity. True
    # only when a semantic fixture was derivable for the task AND the generated
    # handler produced the correct output for every fixture. semantic_checked
    # records whether a fixture was applicable at all — checked=False means the
    # behavioral check was inconclusive, which is NOT a pass.
    semantic_checked: bool = False
    semantic_passed: bool = False
    validation_errors: list[str] = Field(default_factory=list)
    usage_source: EnumUsageSource = Field(
        default=EnumUsageSource.UNKNOWN,
        description=(
            "Provenance of this attempt's token counts, propagated from the LLM "
            "inference response: MEASURED when the provider reported a usage block, "
            "ESTIMATED when derived locally, UNKNOWN when no usage data was "
            "available. Never fabricated — an absent/zero usage block stays UNKNOWN."
        ),
    )


class ModelNodeGenerationRequest(BaseModel):
    """Input command for the generation consumer node.

    OMN-12794: context_pack / context_artifacts + context_pack_hash are the
    typed context-injection seam added in P2-1.  They are OPTIONAL with safe
    defaults so existing callers that omit them continue to work unchanged.

    Do NOT overload previous_errors for context injection — that field is the
    internal repair-loop feedback channel and stays as-is.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_description: str = Field(
        description="Natural language description of the node to generate"
    )
    correlation_id: str = Field(description="Unique run ID for event tracing")
    max_attempts: int = Field(
        default=10, gt=0, description="Maximum LLM retry attempts on validation failure"
    )

    # --- P2-1 context-injection seam (OMN-12794) ---
    # All three fields are optional; omitting them produces baseline (off-arm) behaviour.
    context_pack: str = Field(
        default="",
        description=(
            "Serialised context pack text to prepend to the generation prompt. "
            "Empty string means no context injected (off arm)."
        ),
    )
    context_artifacts: list[ModelContextArtifact] = Field(
        default_factory=list,
        description=(
            "Structured per-artifact provenance for the injected context. "
            "Populated by the runner for audit / replay; the handler uses "
            "context_pack for the actual prompt text."
        ),
    )
    context_pack_hash: str = Field(
        default="",
        description=(
            "SHA-256 hex digest of context_pack content (sha256:<hex>). "
            "Empty string when context_pack is empty. "
            "Used by the scorer to correlate rows with injected content."
        ),
    )

    # --- OMN-13289 (G0): validator-generation acceptance corpus ---
    # Optional with a None default so existing free-text generation callers are
    # unaffected. When present, the run is a *validator generation*: the
    # generated scanner is accepted ONLY if it flags every violation_fixture and
    # passes every clean_fixture (deterministic corpus execution — NOT an LLM
    # self-report). A contract-valid handler that fails the corpus is NOT
    # accepted: it does not deploy, and its corpus failures feed the repair loop.
    validator_corpus: ModelValidatorCorpus | None = Field(
        default=None,
        description=(
            "Fixture corpus that gates acceptance of the generated validator. "
            "None for ordinary free-text node generation (no corpus gate). When "
            "set, acceptance = flags every violation_fixture AND zero findings on "
            "every clean_fixture, by deterministic execution. The corpus is the "
            "acceptance authority, not the LLM (OMN-13289)."
        ),
    )


class ModelGenerationBenchmark(BaseModel):
    """Output benchmark emitted as the terminal event payload.

    OMN-12794 (P2-1): extends the event with provider, prompt/completion token
    split, first-pass flag, and the echoed context pack hash — all sourced from
    typed fields, never from log scraping.  All new fields are OPTIONAL with
    safe defaults (emitter-first, fail-closed: runner must not fake absent fields).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(description="Echoed correlation_id for traceability")
    task_description: str = Field(description="The original task description")
    provider: str = Field(default="", description="LLM provider used")
    model_id: str = Field(default="", description="Model ID used for generation")
    endpoint_class: str = Field(default="", description="Endpoint class (local/cloud)")
    usage_source: EnumUsageSource = Field(
        default=EnumUsageSource.UNKNOWN,
        description=(
            "Aggregated token-usage provenance for the run — typed enum, not a "
            "bare string. Set honestly by the emitter from per-attempt "
            "ModelGenerationAttempt.usage_source: MEASURED when the provider "
            "reported real usage on any attempt, ESTIMATED when only locally "
            "derived, UNKNOWN when no usage data was available. Never fabricated "
            "as MEASURED for an absent/zero usage block."
        ),
    )
    cost_basis: str = Field(default="", description="Cost basis identifier")
    attempts: list[ModelGenerationAttempt] = Field(
        default_factory=list,
        description="Per-attempt details",
    )
    attempt_count: int = Field(default=0, description="Total attempts made")
    total_latency_e2e_ms: int = Field(default=0, description="End-to-end latency in ms")
    contract_passed: bool = Field(
        default=False, description="Whether final output passed validation"
    )
    # OMN-13166: behavioral verdict, separate from contract/shape validity.
    # contract_passed=true means the artifact is shaped like an ONEX node;
    # semantic_passed=true means the generated handler actually performs the
    # requested transformation (verified by executing it against synthesized
    # fixtures). A handler that is shaped correctly but computes the wrong answer
    # is contract_passed=true, semantic_passed=false.
    semantic_checked: bool = Field(
        default=False,
        description=(
            "Whether a behavioral fixture was derivable for the task. False means "
            "the semantic check was inconclusive (no known invariant), which is "
            "NOT a behavioral pass."
        ),
    )
    semantic_passed: bool = Field(
        default=False,
        description=(
            "True only when semantic_checked is true AND the generated handler "
            "produced the correct output for every synthesized fixture. Never "
            "true for an inconclusive (uncheckable) task."
        ),
    )
    # OMN-13289 (G0): validator-generation acceptance verdict, separate from
    # contract/semantic validity. corpus_checked is True only when the request
    # carried a validator_corpus (this run is a validator generation).
    # corpus_passed is True only when the generated scanner flagged every
    # violation_fixture AND produced zero findings on every clean_fixture, by
    # deterministic corpus execution. The corpus — not the LLM — is the
    # acceptance authority. A corpus-checked run that did NOT pass blocks deploy.
    corpus_checked: bool = Field(
        default=False,
        description=(
            "Whether this run carried a validator acceptance corpus. False means "
            "ordinary free-text generation (no corpus gate), which is NOT a "
            "corpus pass."
        ),
    )
    corpus_passed: bool = Field(
        default=False,
        description=(
            "True only when corpus_checked is true AND the generated scanner "
            "flagged every violation_fixture and passed every clean_fixture. "
            "Never true for a run without a corpus."
        ),
    )
    corpus_errors: list[str] = Field(
        default_factory=list,
        description=(
            "Per-fixture acceptance failures (which violation_fixture was missed, "
            "which clean_fixture was false-flagged). Empty on a corpus pass or a "
            "run with no corpus. Fed back into the repair loop."
        ),
    )
    cost_inference_usd: float = Field(
        default=0.0, description="Estimated inference cost in USD"
    )
    reference_chains: list[str] = Field(
        default_factory=list,
        description="Correlation IDs of prior successful generations used as few-shot examples",
    )
    contract_yaml: str = Field(
        default="", description="Generated contract YAML (populated on success)"
    )
    handler_source: str = Field(
        default="", description="Generated handler source (populated on success)"
    )

    # --- P2-1 token split + attempt-reduction telemetry (OMN-12794) ---
    # All optional — safe defaults preserve wire compatibility with existing consumers.
    prompt_tokens: int = Field(
        default=0,
        ge=0,
        description=(
            "Total prompt tokens across all attempts (sum of per-attempt input tokens). "
            "Sourced from typed LLM response fields, not log scraping."
        ),
    )
    completion_tokens: int = Field(
        default=0,
        ge=0,
        description=(
            "Total completion tokens across all attempts (sum of per-attempt output tokens). "
            "Sourced from typed LLM response fields, not log scraping."
        ),
    )
    first_pass_success: bool = Field(
        default=False,
        description=(
            "True when contract validation passed on the very first attempt. "
            "Derived from attempts[0].contract_passed by the emitter. "
            "Distinct from contract_passed (which reflects the final attempt)."
        ),
    )
    context_pack_hash: str = Field(
        default="",
        description=(
            "Echoed from the command's context_pack_hash field. "
            "Enables the runner/scorer to correlate this event with the "
            "injected context pack for the attempt-reduction matrix."
        ),
    )

    # --- OMN-12775 routing-authority proof fields (close-the-loop A3) ---
    # Recorded so the projection can persist what the routing authority actually
    # resolved for this run. These are the evidence the demo acceptance criteria
    # require ("provider, model, endpoint_ref, resolved endpoint, and routing
    # source are recorded — and all resolve from contract / overlay / routing
    # authority"). Empty string is the failed/unresolved sentinel.
    routing_source: str = Field(
        default="",
        description=(
            "Where the endpoint/model decision came from: 'contract' / 'overlay' "
            "/ 'routing_authority'. Declared by the contract model_routing, never "
            "a code literal or env fallback."
        ),
    )
    resolved_endpoint: str = Field(
        default="",
        description=(
            "The COMPLETE endpoint URL the routing authority resolved for this "
            "run, recorded verbatim (no in-code construction). Empty when "
            "generation never reached endpoint resolution."
        ),
    )

    # --- OMN-13356: tool-reuse short-circuit proof ---
    # When the tool-reuse matcher returned a MATCHED verdict before generation,
    # this run was satisfied by an EXISTING tool and NO LLM generation ran. The
    # matched tool's id is recorded here so the short-circuit is provable from the
    # benchmark / projection (zero attempts, zero cost, reused_tool_id set).
    # Empty for an ordinary generation run (no reuse).
    reused_tool_id: str = Field(
        default="",
        description=(
            "Tool id of the existing tool reused for this task (tool-reuse "
            "matcher MATCHED verdict). Empty when generation actually ran. When "
            "set, attempt_count and cost_inference_usd are 0 — the LLM was never "
            "called."
        ),
    )


class ModelNodeDeploy(BaseModel):
    """Payload for the runtime hot-deploy event.

    Consumed by HandlerGeneratedExecutor, which writes the source files to the
    sandbox so the tool is executable without a runtime restart.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_name: str = Field(description="Logical node name (matches sandbox directory)")
    contract_yaml: str = Field(description="Full contract.yaml content")
    handler_source: str = Field(description="Full handler.py content")
    correlation_id: str = Field(description="Correlation ID from the generation run")
    generated_contract_hash: str = Field(
        description="SHA-256 hex digest of contract_yaml (sha256:<hex>)"
    )
    generated_handler_hash: str = Field(
        description="SHA-256 hex digest of handler_source (sha256:<hex>)"
    )
