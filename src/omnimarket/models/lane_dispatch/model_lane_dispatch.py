# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One instruction record for a lane, independent of its transport and caller.

Policy values are declared by the dispatch contract or deployment overlay. The
policy and cited admission snapshot travel with the spec for reproducible replay;
the effect compares them with its authoritative overlay before acting.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from omnimarket.enums.enum_lab_job import EnumLabJobDoneCriterionKind, EnumLabJobEngine
from omnimarket.models.lab_job.model_lab_job_spec import (
    ModelLabJobDoneCriterion,
    ModelLabJobRetryPolicy,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class ModelLaneDispatchHost(_Frozen):
    """A host's declared publication capability; placement never substitutes it."""

    name: str = Field(min_length=1)
    can_handoff: bool


class ModelLaneDispatchModelRequirement(_Frozen):
    """Extra inputs a runner engine requires, declared alongside the engine table."""

    model: EnumLabJobEngine
    fields: tuple[str, ...] = Field(min_length=1)


class ModelLaneDispatchFieldRule(_Frozen):
    """Which engines accept an optional field, and its allowed nonempty values."""

    field: str
    models: tuple[EnumLabJobEngine, ...] = Field(min_length=1)
    allowed_values: tuple[str, ...] | None = None


class ModelLaneDispatchPolicy(_Frozen):
    """Rule parameters, with no deployment facts defaulted in Python."""

    schema_version: Literal["1.0.0"]
    lane_kinds: tuple[str, ...] = Field(min_length=1)
    placements: tuple[str, ...] = Field(min_length=1)
    models: tuple[EnumLabJobEngine, ...] = Field(min_length=1)
    efforts: tuple[str, ...] = Field(min_length=1)
    priority_classes: tuple[str, ...] = Field(min_length=1)
    cloud_placement: str
    cloud_lane_kinds: tuple[str, ...]
    cloud_models: tuple[str, ...]
    umbrella_tickets: tuple[str, ...]
    ack_states: tuple[str, ...]
    merged_ack_state: str
    max_memory_bytes: int = Field(gt=0)
    max_time_box_min: int = Field(gt=0)
    snapshot_max_age_s: int = Field(gt=0)
    hosts: tuple[ModelLaneDispatchHost, ...] = Field(min_length=1)
    publish_steps: tuple[str, ...]
    forbidden_instruction_patterns: tuple[str, ...]
    repository_reference_patterns: tuple[str, ...]
    repository_names: tuple[str, ...]
    allow_cross_repo: bool
    allow_unmerged_dependencies: bool
    model_requirements: tuple[ModelLaneDispatchModelRequirement, ...]
    runner_backoff_s: int = Field(ge=0)
    field_rules: tuple[ModelLaneDispatchFieldRule, ...]
    pr_done_kinds: tuple[EnumLabJobDoneCriterionKind, ...]
    context_done_kinds: tuple[EnumLabJobDoneCriterionKind, ...]
    launching_actors: tuple[str, ...]
    runner_effort_models: tuple[EnumLabJobEngine, ...]

    @model_validator(mode="after")
    def valid_patterns(self) -> Self:
        for pattern in self.forbidden_instruction_patterns:
            re.compile(pattern)
        for pattern in self.repository_reference_patterns:
            if "repo" not in re.compile(pattern).groupindex:
                raise ValueError(
                    "repository reference patterns need a named repo group"
                )
        names = [host.name for host in self.hosts]
        if len(names) != len(set(names)):
            raise ValueError("duplicate host declarations")
        return self


class ModelLaneDispatchPremise(_Frozen):
    """A claim and the actual reader observation supporting it."""

    claim: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


class ModelLaneDispatchDependency(_Frozen):
    """Every source artifact the task depends on, including cross-repo inputs."""

    repo: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    path: str = Field(min_length=1)
    pr: str | None = None
    merged: bool


class ModelLaneDispatchHandoff(_Frozen):
    """Who performs publication and hands the PR to the landing controller."""

    actor: str = Field(min_length=1)
    mode: Literal["lane", "launcher"]


class ModelLaneDispatchClaim(_Frozen):
    """Exact live CLAIM identity; a lane name alone cannot release a newer claim."""

    lane: str = Field(min_length=1)
    stamp: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class ModelLaneDispatchFacts(_Frozen):
    """A cited, bounded read of admission state; unknown state is not empty state."""

    source: str = Field(min_length=1)
    read_at: datetime
    ticket_state: str = Field(min_length=1)
    merged_work: bool
    umbrella: bool
    live_claims: tuple[ModelLaneDispatchClaim, ...]

    @model_validator(mode="after")
    def aware_read(self) -> Self:
        if self.read_at.tzinfo is None:
            raise ValueError("read_at needs a timezone")
        return self


def _refuse(code: str, message: str) -> None:
    raise PydanticCustomError(code, message)


class ModelLaneDispatchSpec(_Frozen):
    """The spec IS the job record; no parallel runner dictionary is persisted."""

    schema_version: Literal["1.0.0"]
    job_id: str = Field(pattern=r"^lj-[0-9a-f]{16}$")
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    ref: str = Field(pattern=r"^[0-9a-f]{40}$")
    pr: tuple[str, ...]
    ticket: str = Field(pattern=r"^OMN-[0-9]+$")
    parent: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$")
    lane: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
    lane_kind: str
    placement: str
    host: str = Field(min_length=1)
    model: EnumLabJobEngine
    effort: str
    priority_class: str
    title: str = Field(min_length=1)
    premises: tuple[ModelLaneDispatchPremise, ...] = Field(min_length=1)
    task: str = Field(min_length=1)
    memory_limit_bytes: int = Field(gt=0)
    publish_steps: tuple[str, ...]
    handoff: ModelLaneDispatchHandoff
    dependencies: tuple[ModelLaneDispatchDependency, ...]
    supersede: tuple[ModelLaneDispatchClaim, ...]
    ticket_state_ack: str | None
    time_box_min: int = Field(gt=0)
    retry_policy: ModelLabJobRetryPolicy
    done_criteria: tuple[ModelLabJobDoneCriterion, ...] = Field(min_length=1)
    risk_class: str | None = None
    escalation_reason: str | None = None
    writable: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    context: tuple[str, ...] = ()
    max_turns: int | None = Field(default=None, ge=1, le=40)
    rules: str = Field(min_length=1)
    constraints: str = Field(min_length=1)
    report: str = Field(min_length=1)
    facts: ModelLaneDispatchFacts
    policy: ModelLaneDispatchPolicy

    @model_validator(mode="before")
    @classmethod
    def required_job_inputs(cls, data: Any) -> Any:
        if isinstance(data, dict):
            missing = [
                key for key in ("repo", "ref", "parent", "ticket") if not data.get(key)
            ]
            if missing:
                _refuse("REMOTE-LANE-JOB-SPEC-REFUSED", "missing " + ", ".join(missing))
        return data

    @model_validator(mode="after")
    def admission(self) -> Self:
        policy = self.policy
        for field, allowed in (
            ("lane_kind", policy.lane_kinds),
            ("placement", policy.placements),
            ("model", policy.models),
            ("effort", policy.efforts),
            ("priority_class", policy.priority_classes),
        ):
            if getattr(self, field) not in allowed:
                _refuse(
                    "DISPATCH-POLICY-REFUSED", f"{field} is not declared by the policy"
                )
        for requirement in policy.model_requirements:
            for field in requirement.fields:
                if field not in type(self).model_fields:
                    _refuse(
                        "DISPATCH-POLICY-REFUSED",
                        "engine requirement names an unknown spec field",
                    )
                if requirement.model == self.model and not getattr(self, field):
                    _refuse(
                        "REMOTE-LANE-MODEL-REFUSED",
                        f"model {self.model} requires {field}",
                    )
        if self.retry_policy.backoff_s != policy.runner_backoff_s:
            _refuse(
                "REMOTE-LANE-JOB-SPEC-REFUSED",
                "retry backoff differs from the declared runner protocol",
            )
        for rule in policy.field_rules:
            if rule.field not in type(self).model_fields:
                _refuse(
                    "DISPATCH-POLICY-REFUSED", "field rule names an unknown spec field"
                )
            value = getattr(self, rule.field)
            if value and (
                self.model not in rule.models
                or (
                    rule.allowed_values is not None and value not in rule.allowed_values
                )
            ):
                _refuse(
                    "REMOTE-LANE-MODEL-REFUSED",
                    f"{rule.field} is not supported for this model/value",
                )
        if self.placement == policy.cloud_placement:
            if self.lane_kind not in policy.cloud_lane_kinds:
                _refuse(
                    "REMOTE-LANE-CLOUD-REFUSED",
                    f"lane kind {self.lane_kind} is not repo-only",
                )
            if self.model not in policy.cloud_models:
                _refuse(
                    "REMOTE-LANE-CLOUD-REFUSED",
                    f"model {self.model} cannot use cloud placement",
                )
        if self.facts.umbrella or self.ticket in policy.umbrella_tickets:
            _refuse(
                "REMOTE-LANE-UMBRELLA-REFUSED",
                "an umbrella ticket cannot own a work lane",
            )
        peers = {
            claim.lane
            for claim in self.facts.live_claims
            if claim.lane != self.lane and claim not in self.supersede
        }
        if peers:
            _refuse(
                "REMOTE-LANE-LIVE-CLAIM-REFUSED",
                "ticket held by live lane " + ", ".join(sorted(peers)),
            )
        ack_state = (
            self.facts.ticket_state
            if self.facts.ticket_state in policy.ack_states
            else policy.merged_ack_state
            if self.facts.merged_work
            else self.facts.ticket_state
        )
        needs_ack = (
            self.facts.merged_work or self.facts.ticket_state in policy.ack_states
        )
        if needs_ack and self.ticket_state_ack != f"{self.ticket}:{ack_state}":
            _refuse(
                "REMOTE-LANE-TICKET-STATE-REFUSED",
                f"needs ticket-state-ack={self.ticket}:{ack_state}",
            )
        if (
            self.ticket_state_ack
            and self.ticket_state_ack != f"{self.ticket}:{ack_state}"
        ):
            _refuse("REMOTE-LANE-TICKET-STATE-REFUSED", "ticket-state-ack is stale")
        if (
            self.memory_limit_bytes > policy.max_memory_bytes
            or self.time_box_min > policy.max_time_box_min
        ):
            _refuse(
                "DISPATCH-BUDGET-REFUSED", "memory or time box exceeds declared budget"
            )
        if any(step not in policy.publish_steps for step in self.publish_steps):
            _refuse("REMOTE-LANE-HANDOFF-REFUSED", "undeclared publish step")
        if (
            self.handoff.mode == "launcher"
            and self.handoff.actor not in policy.launching_actors
        ):
            _refuse(
                "REMOTE-LANE-HANDOFF-REFUSED",
                "launching actor is not declared by the overlay",
            )
        if self.handoff.mode == "lane" and self.handoff.actor != self.lane:
            _refuse(
                "REMOTE-LANE-HANDOFF-REFUSED",
                "lane handoff must name this lane as actor",
            )
        host = next((host for host in policy.hosts if host.name == self.host), None)
        if host is None:
            _refuse(
                "REMOTE-LANE-HOST-REFUSED",
                "named host is not declared; no substitution is permitted",
            )
        if (
            self.publish_steps
            and self.handoff.mode == "lane"
            and (host is None or not host.can_handoff)
        ):
            _refuse(
                "REMOTE-LANE-HANDOFF-REFUSED",
                "named host cannot hand off; name a launching actor",
            )
        for pr in self.pr:
            if not re.fullmatch(
                re.escape(self.repo.split("/", 1)[1]) + r"#[1-9][0-9]*", pr
            ):
                _refuse("DISPATCH-PR-REFUSED", "PR must belong to the spec repository")
        for criterion in self.done_criteria:
            if bool(criterion.target) != (
                criterion.kind in policy.pr_done_kinds
            ) or bool(criterion.check_context) != (
                criterion.kind in policy.context_done_kinds
            ):
                _refuse(
                    "REMOTE-LANE-JOB-SPEC-REFUSED",
                    "done criterion fields differ from the declared runner protocol",
                )
            if criterion.target and not re.fullmatch(
                re.escape(self.repo) + r"#[1-9][0-9]*", criterion.target
            ):
                _refuse(
                    "DISPATCH-PR-REFUSED",
                    "done criterion target must belong to the spec repository",
                )
        for match in re.finditer(
            r"(?m)^Required host:\s*(\S+)",
            "\n".join((self.task, self.rules, self.constraints, self.report)),
        ):
            if match.group(1) != self.host:
                _refuse(
                    "REMOTE-LANE-HOST-REFUSED",
                    "brief text and spec name different hosts",
                )
        model_line = re.search(r"(?m)^model=(\S+) effort=(\S+)", self.constraints)
        if model_line is None or model_line.groups() != (self.model.value, self.effort):
            _refuse(
                "DISPATCH-IDENTITY-REFUSED",
                "generated 3a model/effort differs from spec; regenerate constraints",
            )
        claim_section = re.search(
            r"(?ms)^### 3a\.8\b[^\n]*\n(.*?)(?=^### |\Z)", self.constraints
        )
        if claim_section is None:
            _refuse(
                "DISPATCH-IDENTITY-REFUSED",
                "generated constraints need the 3a.8 CLAIM section",
            )
        claim_text = claim_section.group(1) if claim_section is not None else ""
        declared_stamps = {claim.stamp for claim in self.supersede}
        if set(re.findall(r"supersedes-claim=(\S+)", claim_text)) - declared_stamps:
            _refuse(
                "DISPATCH-IDENTITY-REFUSED",
                "constraints contain an undeclared supersession",
            )
        for field, expected in (
            ("lane", self.lane),
            ("ticket", self.ticket),
            ("parent", self.parent),
            ("repo", self.repo.split("/", 1)[1]),
        ):
            values = re.findall(r"(?:^| \| )" + field + r"=([^ |\n]+)", claim_text)
            if not values or any(value != expected for value in values):
                _refuse(
                    "DISPATCH-IDENTITY-REFUSED",
                    f"generated CLAIM {field} differs from spec; regenerate constraints",
                )
        for dependency in self.dependencies:
            if not policy.allow_cross_repo and dependency.repo != self.repo:
                _refuse(
                    "REMOTE-LANE-DEPENDENCY-REFUSED",
                    "task depends on another repository",
                )
            if (
                not policy.allow_unmerged_dependencies
                and dependency.pr
                and not dependency.merged
            ):
                _refuse(
                    "REMOTE-LANE-DEPENDENCY-REFUSED", "task depends on an unmerged PR"
                )
        instructions = "\n".join((self.title, self.task, *self.publish_steps))
        for pattern in policy.forbidden_instruction_patterns:
            if re.search(pattern, instructions, re.IGNORECASE | re.MULTILINE):
                _refuse(
                    "LANE-BRIEF-REFUSED",
                    "task contains an instruction refused by declared brief policy",
                )
        if not policy.allow_cross_repo:
            for pattern in policy.repository_reference_patterns:
                for match in re.finditer(pattern, instructions):
                    if match.group("repo") != self.repo:
                        _refuse(
                            "REMOTE-LANE-DEPENDENCY-REFUSED",
                            "task cites another repository",
                        )
            for name in policy.repository_names:
                if name not in (self.repo, self.repo.split("/", 1)[1]) and re.search(
                    r"(?<![\w.-])" + re.escape(name) + r"(?![\w.-])", instructions
                ):
                    _refuse(
                        "REMOTE-LANE-DEPENDENCY-REFUSED",
                        "task names another repository",
                    )
        return self

    def canonical_json(self) -> str:
        """UTF-8, sorted object keys, compact separators, no non-finite numbers."""
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class ModelLaneDispatchRecord(_Frozen):
    """Content addressed envelope: receipts cite content_hash and job_id."""

    spec: ModelLaneDispatchSpec
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_spec(cls, spec: ModelLaneDispatchSpec) -> Self:
        return cls(spec=spec, content_hash=spec.content_hash())

    @model_validator(mode="after")
    def verify_hash(self) -> Self:
        if self.content_hash != self.spec.content_hash():
            _refuse("DISPATCH-HASH-MISMATCH", "spec does not match its content hash")
        return self

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
