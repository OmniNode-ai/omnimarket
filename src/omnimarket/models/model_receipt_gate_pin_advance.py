# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed I/O and policy data for the receipt-gate pin advance (OMN-20134).

The policy is contract data: the callers' pin sites are declared once in the
node's ``contract.yaml``, never in code. A pin site is a file path and a regex
with a named group ``ref`` over the pinned ref (and, for a ``sha`` site that
names its dev tag beside the sha, an optional group ``tag``).
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEV_TAG_PATTERN = r"^dev-(?P<stamp>\d{14})-(?P<sha12>[0-9a-f]{12})$"
_SHA40 = r"^[0-9a-f]{40}$"


class EnumPinForm(StrEnum):
    """What a pin site holds: the dev-merge tag itself, or the tag's commit sha."""

    TAG = "tag"
    SHA = "sha"


class EnumCallerVerdict(StrEnum):
    """What the planner found at one caller. Only CURRENT needs no pull request."""

    CURRENT = "current"
    STALE = "stale"
    UNTAGGED = "untagged"
    UNRESOLVED = "unresolved"
    SITES_DISAGREE = "sites_disagree"
    PIN_SITE_UNMATCHED = "pin_site_unmatched"
    SNAPSHOT_MISSING = "snapshot_missing"


class ModelPinSite(BaseModel):
    """One place a caller holds the pin: a workflow line or a test constant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    pattern: str = Field(
        min_length=1,
        description="Regex with the named group 'ref' (and optionally 'tag').",
    )
    ref_form: EnumPinForm
    expected_matches: int = Field(default=1, ge=1)

    @field_validator("pattern")
    @classmethod
    def _pattern_names_the_ref_group(cls, value: str) -> str:
        compiled = re.compile(value)
        if "ref" not in compiled.groupindex:
            raise ValueError("pattern must name a group 'ref'")
        return value


class ModelPinCaller(BaseModel):
    """One repository's receipt-gate caller: its base branch and its pin sites."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base_branch: str = Field(min_length=1)
    pin_sites: tuple[ModelPinSite, ...] = Field(min_length=1)
    test_constants: tuple[ModelPinSite, ...] = ()


class ModelPinExclusion(BaseModel):
    """A repository that calls the gate and is deliberately not advanced here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    reason: str = Field(min_length=1)


class ModelReceiptGatePinPolicy(BaseModel):
    """The pin-advance policy declared in the node's contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    workflow_path: str = Field(min_length=1)
    ticket: str = Field(pattern=r"^OMN-\d+$")
    branch_prefix: str = Field(pattern=r"^[A-Za-z0-9._/-]+$")
    callers: tuple[ModelPinCaller, ...] = Field(min_length=1)
    excluded: tuple[ModelPinExclusion, ...] = ()


class ModelCoreDevTag(BaseModel):
    """One dev-merge tag of the gate's source repository and its gate blob."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tag: str = Field(pattern=DEV_TAG_PATTERN)
    commit_sha: str = Field(pattern=_SHA40)
    receipt_gate_blob: str = Field(
        pattern=_SHA40,
        description="The git blob id of the gate workflow file at this tag.",
    )


class ModelCallerSnapshot(BaseModel):
    """The text of a caller's pin-site files at its base branch head."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    base_head_sha: str = Field(pattern=_SHA40)
    files: dict[str, str]


class ModelReceiptGatePinAdvanceRequest(BaseModel):
    """The declared policy, the source repository's dev tags and every caller's files."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy: ModelReceiptGatePinPolicy
    core_tags: tuple[ModelCoreDevTag, ...] = Field(min_length=1)
    callers: tuple[ModelCallerSnapshot, ...]


class ModelFileEdit(BaseModel):
    """A file's full new text; one commit carries every edit of a plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    new_text: str


class ModelCallerPlan(BaseModel):
    """What to do at one caller: nothing, or one pin pull request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    verdict: EnumCallerVerdict
    detail: str = ""
    base_branch: str = ""
    base_head_sha: str = ""
    current_ref: str | None = None
    current_tag: str | None = None
    target_tag: str | None = None
    target_commit_sha: str | None = None
    edits: tuple[ModelFileEdit, ...] = ()
    branch: str = ""
    title: str = ""
    body: str = ""
    commit_message: str = ""
    idempotency_key: str = ""

    @property
    def needs_pull_request(self) -> bool:
        return bool(self.edits)


class ModelReceiptGatePinAdvanceResult(BaseModel):
    """One plan per declared caller, the newest tag, and the callers left out."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    newest_tag: str
    newest_receipt_gate_blob: str
    plans: tuple[ModelCallerPlan, ...]
    excluded: tuple[ModelPinExclusion, ...] = ()

    @property
    def violations(self) -> tuple[ModelCallerPlan, ...]:
        """Every caller whose pin is not current, or could not be judged."""
        return tuple(
            p for p in self.plans if p.verdict is not EnumCallerVerdict.CURRENT
        )

    @property
    def advances(self) -> tuple[ModelCallerPlan, ...]:
        """The plans that open a pull request."""
        return tuple(p for p in self.plans if p.needs_pull_request)
