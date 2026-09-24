# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed access to the Market-owned task-class and Gateway exposure authority."""

from __future__ import annotations

import re
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

import yaml
from omnibase_core.models.delegation.wire import EnumDelegationOutputShape
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

_DEFAULT_AUTHORITY_PATH = (
    Path(__file__).resolve().parent.parent / "configs" / "task_class_contracts.v1.yaml"
)


class EnumGatewayExposure(StrEnum):
    """Closed Gateway exposure policy for a Market task class."""

    PUBLIC = "public"
    INTERNAL = "internal"


class EnumRoutingAvailabilityStatus(StrEnum):
    """Why a declared task class resolves no backend on any tier (OMN-16811)."""

    #: No tier can supply a capability the class requires (agent_delegation).
    PENDING_CAPABILITY = "pending_capability"


class EnumTaskTypeResolution(StrEnum):
    """How a delegation's task class was decided (OMN-18305, OMN-19407).

    Recorded on stderr and on every run artifact, so a class the caller did
    not choose is never applied silently.
    """

    #: The caller named the class. Never second-guessed.
    EXPLICIT = "explicit"
    #: A declared selection predicate claimed the prompt.
    CONTRACT = "contract"
    #: No predicate claimed it; the declared ``selection_fallback`` was used.
    FALLBACK = "fallback"


class TaskClassSelectionError(ValueError):
    """A task class could not be resolved from this authority.

    Raised for an explicit class the authority does not declare, for a class
    it declares unroutable, and for an unclaimed prompt when the authority
    declares no fallback. Never defaulted: a class nobody declared is the
    defect OMN-18305 removed.
    """


class EnumQualityRuleEnforcement(StrEnum):
    """Who a declared quality rule answers to (OMN-18295).

    A rule is one of these, never both — that duality is the defect this enum
    exists to make unrepresentable.
    """

    #: Contributes to the graded score; the task class's ``required_bar`` is
    #: the single authority on the verdict.
    SCORED = "scored"
    #: Vetoes acceptance outright, whatever the score. Floors and adequacy
    #: authorities only.
    BLOCKING = "blocking"


class ModelQualityRule(BaseModel):
    """One declared quality rule: its threshold and its enforcement class."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enforcement: EnumQualityRuleEnforcement
    threshold: int | None = Field(
        default=None,
        description=(
            "The rule's numeric threshold, where it has one. Declared here so "
            "a customer is never held to a number that appears in no contract "
            "and on no receipt (OMN-18295)."
        ),
    )
    threshold_unit: str | None = Field(
        default=None,
        description="What the threshold counts — 'words', 'characters'.",
    )
    rationale: str = Field(
        min_length=1,
        description=(
            "Why this rule is scored or blocking. Required: an enforcement "
            "class with no stated reason is the thing that drifts."
        ),
    )
    model_directive: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "What the model is asked to do so that its answer meets this rule, "
            "stated in the request it answers (OMN-18349). A blocking rule the "
            "model is never told about refuses correct answers: on the lab, "
            "every local research and code-review answer was refused on a "
            "citation rule nothing in the request mentioned. None means the "
            "rule is not stated to the model."
        ),
    )


class ModelReasoningPreamblePolicy(BaseModel):
    """Where a leaked plain-text reasoning scratchpad ends (OMN-18379).

    The search order and the reason each entry exists are documented in the
    ``reasoning_preamble`` block of ``task_class_contracts.v1.yaml``. The
    values live there rather than in Python so the phrases a customer's
    response is segmented on are a readable contract.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lead_in_phrases: tuple[str, ...] = Field(
        min_length=1,
        description=(
            "Phrases that, at the START of a response, mark it as opening with "
            "a reasoning scratchpad. Required before the structural boundaries "
            "(marker / header / fence) are consulted at all."
        ),
    )
    answer_markers: tuple[str, ...] = Field(
        min_length=1,
        description=(
            "Explicit answer markers a prompt can request. A line equal to one "
            "of these ends the preamble."
        ),
    )
    closing_trace_tags: tuple[str, ...] = Field(
        min_length=1,
        description=(
            "Reasoning-trace terminators. One with no matching opener ends the "
            "preamble on its own, with no lead-in required."
        ),
    )
    rationale: str = Field(
        min_length=1,
        description="Why this policy is shaped the way it is.",
    )


class ModelQualifiedPhrases(BaseModel):
    """Phrases that claim a prompt only with a declared qualifier nearby (OMN-18831).

    WHY THIS EXISTS. Some of the most useful selection phrases are ordinary
    English before they are technical. "write a" opens almost any
    instruction-shaped request, and "assertion" is the ordinary word for a
    claim. Declared as plain `phrases` they routed prose to classes whose
    acceptance is DETERMINISTIC, so the answer was graded on the wrong thing
    entirely: a 388-word request opening "Write a GitHub PR body in markdown"
    was claimed by `code_generation` on the two-word match, and five rungs in
    a row returned correct English that was refused for not compiling as
    Python (run 21b33edf-32aa-4279-9c1b-52b034c2ee9e, 2026-09-19).

    WHY NOT DELETE THE PHRASE. "write a parser" is a code request and "add
    assertions to the auth tests" is a test request, and no other declared
    phrase claims either. Deletion trades one misroute for another.

    WHAT IS DECLARED. The counter-signal is the OBJECT of the verb: a gated
    phrase counts only where one of `qualifiers` occurs within `within_words`
    words before or after it. The qualifier set is closed -- the artifacts a
    request can name -- where "every way prose can be phrased" is not, so the
    gate is stated positively rather than as a guessed-at list of exclusions.
    The vocabulary IS the word sense, which is why it is contract data here
    and not a table inside whichever consumer evaluates it.

    Each field is required and non-empty. A block with no qualifiers, no
    phrases, or a zero-word window degrades to "matches unconditionally" --
    the very defect the field removes -- so it is refused at load.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    within_words: int = Field(
        ge=1,
        description=(
            "How many words either side of the phrase are searched for a qualifier."
        ),
    )
    phrases: tuple[str, ...] = Field(
        min_length=1,
        description="The ambiguous phrases, matched on word boundaries then gated.",
    )
    qualifiers: tuple[str, ...] = Field(
        min_length=1,
        description=(
            "Terms whose presence near a phrase settles its word sense. "
            "Word-boundary matched, so multi-word qualifiers work."
        ),
    )

    @field_validator("phrases", "qualifiers")
    @classmethod
    def _validate_terms(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        invalid = sorted(
            term for term in value if not term or term != term.strip().lower()
        )
        if invalid:
            raise ValueError(
                f"terms must be non-empty, trimmed and lowercase: {invalid}"
            )
        return value


class ModelTaskClassExecutionBudget(BaseModel):
    """Declared ceiling and terminal margin for one task class.

    The ceiling stays at or below 240 seconds, under the deployed dispatch
    port's 300-second wait, so the handler's own deadline fires before the
    port gives up on it. The delivery margin belongs to the terminal waiter,
    not the model execution deadline. (The bound moved here from the onex
    CLI's mirror model, OMN-19407.)
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_class_timeout_ceiling_seconds: int = Field(ge=1, le=240)
    terminal_delivery_margin_seconds: int = Field(ge=1)


class ModelTaskClassOutputContract(BaseModel):
    """Default output boundary that applies when a caller declares no schema."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    output_shape: EnumDelegationOutputShape
    start_marker: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _validate_boundary(self) -> ModelTaskClassOutputContract:
        if self.output_shape is EnumDelegationOutputShape.JSON:
            if self.start_marker is not None:
                raise ValueError("json output contract must not declare a start marker")
        elif self.start_marker is None:
            raise ValueError("text output contract requires a declared start marker")
        return self


class ModelDelegationOutputAuthority(BaseModel):
    """Declared extraction floors and markers shared by delegated task classes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_deliverable_share: float = Field(gt=0.0, le=1.0)
    markdown_markers: tuple[str, ...] = Field(min_length=1)
    plain_text_markers: tuple[str, ...] = Field(min_length=1)

    def markers_for(self, output_shape: EnumDelegationOutputShape) -> tuple[str, ...]:
        """Return the declared boundary markers for a text output shape."""
        if output_shape is EnumDelegationOutputShape.MARKDOWN:
            return self.markdown_markers
        if output_shape is EnumDelegationOutputShape.PLAIN_TEXT:
            return self.plain_text_markers
        return ()


class ModelTaskClassSelection(BaseModel):
    """How a prompt selects this task class (OMN-18305).

    This is the contract half of task-class SELECTION. Before OMN-18305 there
    was no contract half at all: the `onex delegate` CLI carried a hardcoded,
    ordered keyword table lifted from retired skill markdown, matched with a
    bare substring test, first rule wins. A 56 KB engineering standup was
    therefore filed as a test-writing task because the material it summarised
    contained the word "test" — and `test` is not one of the classes whose
    quality bar arms the prose checks, so the identifier-grounding check and
    the prose quality band never ran on the customer's answer. The four-word
    prompt "the latest window" classified as `test` for the same reason:
    "latest" contains "test".

    Three properties are load-bearing, and each is a field here rather than an
    implementation detail of whichever consumer evaluates it:

    * **Presence, never frequency.** A phrase either occurs or it does not.
      Counting occurrences is what let the bulk of a document outvote its
      purpose.
    * **Word boundaries.** Phrases match on word boundaries only, so no phrase
      can match inside a longer word.
    * **Shape gates the keyword.** ``min_words`` / ``max_words`` bound the
      prompt shapes a class is eligible for at all, evaluated BEFORE any
      phrase. A long prose task is structurally ineligible for the short
      keyword-driven classes, whatever words it happens to contain.

    ``priority`` breaks ties between eligible classes (higher wins; equal
    priorities are broken by class name so resolution is total and
    deterministic). An empty ``phrases`` list means this class is never
    resolved from a prompt — the explicit, typed way to say so for the
    internal classes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    priority: int = Field(
        ge=0,
        description=(
            "Rank among eligible classes; higher wins. The prose classes sit "
            "above the keyword classes so prompt shape outranks one "
            "incidental keyword."
        ),
    )
    phrases: tuple[str, ...] = Field(
        description=(
            "Word-boundary phrases whose PRESENCE makes this class eligible. "
            "Empty means never selected from a prompt."
        ),
    )
    min_words: int | None = Field(
        default=None,
        ge=1,
        description="Shortest prompt, in words, this class is eligible for.",
    )
    max_words: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Longest prompt, in words, this class is eligible for. This is "
            "the bound that makes a 7,000-word ledger structurally ineligible "
            "for `test`."
        ),
    )

    qualified_phrases: ModelQualifiedPhrases | None = Field(
        default=None,
        description=(
            "Phrases too ambiguous to claim a prompt on their own; see "
            "`ModelQualifiedPhrases`. Absent means this class declares none, "
            "which is every class written before OMN-18831 and most since."
        ),
    )

    @field_validator("phrases")
    @classmethod
    def _validate_phrases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        invalid = sorted(
            phrase for phrase in value if not phrase or phrase != phrase.strip().lower()
        )
        if invalid:
            raise ValueError(
                f"selection phrases must be non-empty, trimmed and lowercase: {invalid}"
            )
        return value

    def shape_admits(self, word_count: int) -> bool:
        """Return whether a prompt of ``word_count`` words is eligible at all."""
        if self.min_words is not None and word_count < self.min_words:
            return False
        return not (self.max_words is not None and word_count > self.max_words)

    def matching_phrase(self, lowered_prompt: str) -> str | None:
        """Return the most specific declared phrase that claims this prompt.

        Longest first, then alphabetically, so the reason line names the phrase
        that describes the request rather than whichever one YAML order put
        first. A phrase under ``qualified_phrases`` claims the prompt only at an
        occurrence with a qualifier within the declared window. Every
        occurrence is considered, not only the first: one unqualified use must
        not veto a later qualified one, or frequency would decide the answer.
        """
        gated = self.qualified_phrases
        gated_phrases = gated.phrases if gated is not None else ()
        for phrase in sorted(
            (*self.phrases, *gated_phrases), key=lambda item: (-len(item), item)
        ):
            for occurrence in _phrase_pattern(phrase).finditer(lowered_prompt):
                if phrase not in gated_phrases or _qualifier_near(
                    lowered_prompt, occurrence.span(), gated
                ):
                    return phrase
        return None


#: One run of non-space characters: ``within_words`` counts words, not characters.
_WORD = re.compile(r"\S+")


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Return the word-boundary matcher for one declared phrase.

    ``(?<!\\w)`` / ``(?!\\w)`` rather than ``\\b`` so a phrase ending in
    punctuation still matches. The OMN-18305 defect was a bare substring test
    ("latest" contains "test").
    """
    return re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)")


def _qualifier_near(
    lowered_prompt: str,
    span: tuple[int, int],
    gated: ModelQualifiedPhrases | None,
) -> bool:
    """Return whether a declared qualifier sits within the declared window.

    The phrase's own span is excluded, so a phrase is never its own
    counter-signal.
    """
    if gated is None:
        return False
    start, end = span
    word_starts = [match.start() for match in _WORD.finditer(lowered_prompt, 0, start)]
    word_ends = [match.end() for match in _WORD.finditer(lowered_prompt, end)]
    window = gated.within_words
    left = word_starts[-window] if len(word_starts) >= window else 0
    right = word_ends[window - 1] if len(word_ends) >= window else len(lowered_prompt)
    before, after = lowered_prompt[left:start], lowered_prompt[end:right]
    return any(
        _phrase_pattern(qualifier).search(before) is not None
        or _phrase_pattern(qualifier).search(after) is not None
        for qualifier in gated.qualifiers
    )


class ModelRoutingAvailability(BaseModel):
    """A class the contract declares but cannot route yet (OMN-16811).

    Declared so that every consumer refuses the class up front, in these
    words, instead of dispatching it and waiting out the ingress budget.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumRoutingAvailabilityStatus
    missing_capability: str = Field(min_length=1)
    tracking: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ModelSelectionFallback(BaseModel):
    """The class an unclaimed prompt resolves to (OMN-18305, OMN-19407).

    A GRADING decision, so the contract owns it and no consumer carries a
    default of its own. The declared class must be public; the loader refuses
    anything else.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_class: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class ModelTaskTypeResolution(BaseModel):
    """The resolved class, how it was resolved, and why: all three on the record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_type: str = Field(min_length=1)
    resolution: EnumTaskTypeResolution
    reason: str = Field(min_length=1)


class ModelTaskClassAuthorityEntry(BaseModel):
    """Authority fields shared by every task-class routing contract entry."""

    model_config = ConfigDict(frozen=True, extra="allow")

    gateway_exposure: EnumGatewayExposure
    selection: ModelTaskClassSelection
    output_contract: ModelTaskClassOutputContract | None = Field(default=None)
    routing_availability: ModelRoutingAvailability | None = Field(
        default=None,
        description=(
            "Present only on a class no tier can route yet. Absent means the "
            "class routes."
        ),
    )


class ModelTaskClassAuthority(BaseModel):
    """Market-owned task-class universe with a total Gateway exposure partition."""

    model_config = ConfigDict(frozen=True, extra="allow")

    task_classes: dict[str, ModelTaskClassAuthorityEntry] = Field(min_length=1)
    quality_rules: dict[str, ModelQualityRule] = Field(default_factory=dict)
    reasoning_preamble: ModelReasoningPreamblePolicy | None = Field(
        default=None,
        description=(
            "How a leaked reasoning scratchpad is separated from the answer "
            "(OMN-18379). ``None`` means no segmentation is performed and the "
            "whole response is verified, which is the pre-ticket behaviour."
        ),
    )
    delegation_output: ModelDelegationOutputAuthority | None = Field(default=None)
    execution_budgets: dict[str, ModelTaskClassExecutionBudget] = Field(
        default_factory=dict
    )
    selection_fallback: ModelSelectionFallback | None = Field(
        default=None,
        description=(
            "The class an unclaimed prompt resolves to. Absent means an "
            "unclaimed prompt is refused and the caller must name a class."
        ),
    )

    @model_validator(mode="after")
    def _validate_selection_fallback(self) -> ModelTaskClassAuthority:
        fallback = self.selection_fallback
        if fallback is not None and fallback.task_class not in self.public_task_classes:
            raise ValueError(
                f"selection_fallback names {fallback.task_class!r}, which is not "
                "a public task class; a prompt could never be routed there. "
                f"Public: {', '.join(sorted(self.public_task_classes))}"
            )
        return self

    @field_validator("task_classes")
    @classmethod
    def _validate_task_class_names(
        cls,
        value: dict[str, ModelTaskClassAuthorityEntry],
    ) -> dict[str, ModelTaskClassAuthorityEntry]:
        invalid = sorted(name for name in value if not name or name != name.strip())
        if invalid:
            raise ValueError(
                f"task class names must be non-empty and trimmed: {invalid}"
            )
        return value

    @property
    def universe(self) -> frozenset[str]:
        """Return the registry keys, which are the sole task-class denominator."""
        return frozenset(self.task_classes)

    @property
    def public_task_classes(self) -> frozenset[str]:
        """Return the exact public Gateway projection."""
        return self._classes_with_exposure(EnumGatewayExposure.PUBLIC)

    @property
    def internal_task_classes(self) -> frozenset[str]:
        """Return valid Market classes excluded from the public Gateway."""
        return self._classes_with_exposure(EnumGatewayExposure.INTERNAL)

    @property
    def unroutable_task_classes(self) -> dict[str, ModelRoutingAvailability]:
        """Return every declared class no tier can route yet, with its declaration."""
        return {
            name: entry.routing_availability
            for name, entry in self.task_classes.items()
            if entry.routing_availability is not None
        }

    def _classes_with_exposure(
        self,
        exposure: EnumGatewayExposure,
    ) -> frozenset[str]:
        return frozenset(
            name
            for name, entry in self.task_classes.items()
            if entry.gateway_exposure is exposure
        )

    def execution_budget(self, task_class: str) -> ModelTaskClassExecutionBudget:
        """Return the declared handler budget for ``task_class``, or refuse."""
        try:
            return self.execution_budgets[task_class]
        except KeyError as exc:
            raise ValueError(
                f"no declared execution budget for task class: {task_class}"
            ) from exc

    def unroutable_refusal(self, task_class: str) -> str | None:
        """Return the refusal for a class declared unroutable, in the contract's words."""
        declared = self.unroutable_task_classes.get(task_class)
        if declared is None:
            return None
        return (
            f"task class {task_class!r} is declared by the task-class contract "
            f"but not routable: routing_availability status "
            f"{declared.status.value!r}, missing capability "
            f"{declared.missing_capability!r} (tracking: {declared.tracking}). "
            f"{' '.join(declared.reason.split())}"
        )

    def resolve_task_type(
        self, prompt: str, *, explicit: str | None
    ) -> ModelTaskTypeResolution:
        """Resolve a delegation's task class and record how it was decided.

        An explicit class may name any class this authority declares, public
        or internal: ``gateway_exposure`` governs the public Gateway and
        auto-selection, not a caller who names a class (OMN-13966). A class
        declared unroutable is refused in its own declaration's words, and a
        class not declared at all is refused naming every class that is.

        Without an explicit class only public classes are eligible. Shape
        gates the phrase, phrases match on word boundaries by presence only,
        and ties go to the higher priority and then the class name, so the
        answer is total and deterministic. An unclaimed prompt resolves to the
        declared ``selection_fallback``.

        Raises:
            TaskClassSelectionError: the class cannot be resolved.
        """
        if explicit is not None:
            refusal = self.unroutable_refusal(explicit)
            if refusal is not None:
                raise TaskClassSelectionError(refusal)
            if explicit in self.public_task_classes:
                return ModelTaskTypeResolution(
                    task_type=explicit,
                    resolution=EnumTaskTypeResolution.EXPLICIT,
                    reason="explicitly selected with --task-type",
                )
            if explicit in self.internal_task_classes:
                return ModelTaskTypeResolution(
                    task_type=explicit,
                    resolution=EnumTaskTypeResolution.EXPLICIT,
                    reason=(
                        "explicitly selected with --task-type; an internal "
                        "class, never chosen for a prompt and not admitted at "
                        "the public Gateway"
                    ),
                )
            raise TaskClassSelectionError(
                f"unknown task type {explicit!r}; the task-class contract "
                f"declares public: {', '.join(sorted(self.public_task_classes))}; "
                "internal, by explicit name: "
                f"{', '.join(sorted(self.internal_task_classes - self.unroutable_task_classes.keys())) or '(none)'}; "
                "declared but not routable: "
                f"{', '.join(sorted(self.unroutable_task_classes)) or '(none)'}"
            )

        lowered = prompt.lower()
        word_count = len(prompt.split())
        eligible: list[tuple[int, str, str]] = []
        for name in self.public_task_classes:
            selection = self.task_classes[name].selection
            if not selection.shape_admits(word_count):
                continue
            phrase = selection.matching_phrase(lowered)
            if phrase is not None:
                eligible.append((selection.priority, name, phrase))

        if not eligible:
            fallback = self.selection_fallback
            if fallback is None:
                raise TaskClassSelectionError(
                    f"no declared selection predicate claimed this {word_count}-"
                    "word prompt and the task-class contract declares no "
                    "selection_fallback; pass --task-type"
                )
            return ModelTaskTypeResolution(
                task_type=fallback.task_class,
                resolution=EnumTaskTypeResolution.FALLBACK,
                reason=(
                    f"no declared selection predicate claimed this "
                    f"{word_count}-word prompt; using the contract's declared "
                    f"selection_fallback {fallback.task_class!r}. Pass "
                    "--criteria to state your own acceptance criteria instead"
                ),
            )

        priority, name, phrase = min(eligible, key=lambda item: (-item[0], item[1]))
        return ModelTaskTypeResolution(
            task_type=name,
            resolution=EnumTaskTypeResolution.CONTRACT,
            reason=(
                f"contract predicate for {name!r} (priority {priority}) matched "
                f"the phrase {phrase!r} in a {word_count}-word prompt"
            ),
        )


def load_task_class_authority(
    config_path: str | Path | None = None,
) -> ModelTaskClassAuthority:
    """Load the Market authority and fail closed on missing/unknown exposure."""
    path = Path(config_path) if config_path is not None else _DEFAULT_AUTHORITY_PATH
    if not path.is_file():
        raise FileNotFoundError(f"task-class authority not found at {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"task-class authority root must be a mapping: {path}")
    try:
        return ModelTaskClassAuthority.model_validate(raw)
    except ValidationError as exc:
        msg = f"task-class authority validation failed for {path}: {exc}"
        raise ValueError(msg) from exc


@lru_cache(maxsize=1)
def _quality_rules() -> dict[str, ModelQualityRule]:
    """The declared quality-rule registry, read once.

    Cached because the quality gate is a pure reducer called per delegation
    and per declared check: re-reading and re-validating the authority file
    inside that loop would put file I/O on a hot path that is meant to be
    deterministic and free.

    A missing or malformed authority file yields an EMPTY registry rather than
    raising. The gate's job is to judge a response, and it must not start
    failing every delegation because a config read broke — see
    :func:`resolve_quality_rule` for what an unresolved rule then means.
    """
    try:
        return dict(load_task_class_authority().quality_rules)
    except (FileNotFoundError, ValueError):
        return {}


def resolve_quality_rule(name: str) -> ModelQualityRule | None:
    """Return the declared rule for ``name``, or ``None`` if undeclared.

    ``None`` is a real answer, not an error: a check the quality gate knows
    how to run but the contract has not classified. The gate treats an
    unresolved rule as BLOCKING — the pre-OMN-18295 behaviour for every
    heuristic check. Fail-closed is the right default here because the
    alternative is that forgetting to declare a rule silently strips its
    veto, which is precisely the class of quiet weakening this ticket is
    about.
    """
    return _quality_rules().get(name)


@lru_cache(maxsize=1)
def resolve_reasoning_preamble_policy() -> ModelReasoningPreamblePolicy | None:
    """The declared reasoning-preamble segmentation policy, or ``None``.

    ``None`` is a real answer: the authority file is absent, unreadable, or
    declares no ``reasoning_preamble`` block. The segmenter then finds no
    boundary, the whole response is verified exactly as it was before
    OMN-18379, and the receipt says ``no_boundary_found``. Failing closed here
    means NOT cutting: a segmenter that guessed a boundary from an unreadable
    contract would silently drop a customer's answer, which is a far worse
    outcome than the veto this ticket exists to remove.

    Cached for the same reason ``_quality_rules`` is: the gate is a pure
    reducer called per delegation and per declared check.
    """
    try:
        return load_task_class_authority().reasoning_preamble
    except (FileNotFoundError, ValueError):
        return None


def resolve_task_class_execution_budget(
    task_class: str,
) -> ModelTaskClassExecutionBudget:
    """Return the explicit handler budget for ``task_class`` or refuse dispatch."""
    return load_task_class_authority().execution_budget(task_class)


def resolve_delegation_output_authority() -> ModelDelegationOutputAuthority:
    """Return output extraction authority or refuse a contract-governed response."""
    authority = load_task_class_authority()
    if authority.delegation_output is None:
        raise ValueError("delegation output authority is not declared")
    return authority.delegation_output


def resolve_task_class_output_contract(task_class: str) -> ModelTaskClassOutputContract:
    """Return a task-class default output contract or refuse an unbounded response."""
    authority = load_task_class_authority()
    try:
        entry = authority.task_classes[task_class]
    except KeyError as exc:
        raise ValueError(f"unknown task class: {task_class}") from exc
    if entry.output_contract is None:
        raise ValueError(f"no default output contract for task class: {task_class}")
    return entry.output_contract


__all__ = [
    "EnumGatewayExposure",
    "EnumQualityRuleEnforcement",
    "EnumRoutingAvailabilityStatus",
    "EnumTaskTypeResolution",
    "ModelDelegationOutputAuthority",
    "ModelQualifiedPhrases",
    "ModelQualityRule",
    "ModelReasoningPreamblePolicy",
    "ModelRoutingAvailability",
    "ModelSelectionFallback",
    "ModelTaskClassAuthority",
    "ModelTaskClassAuthorityEntry",
    "ModelTaskClassExecutionBudget",
    "ModelTaskClassOutputContract",
    "ModelTaskClassSelection",
    "ModelTaskTypeResolution",
    "TaskClassSelectionError",
    "load_task_class_authority",
    "resolve_delegation_output_authority",
    "resolve_quality_rule",
    "resolve_reasoning_preamble_policy",
    "resolve_task_class_execution_budget",
    "resolve_task_class_output_contract",
]
