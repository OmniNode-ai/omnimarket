# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared PR-state event vocabulary (OMN-19999).

The PR-state emit node produces these models and the PR-state projection node
consumes them, so they live in the shared ``omnimarket.events`` package rather
than in either node.

Canonical JSON for the observed event uses sorted keys, compact separators,
UTF-8 (unescaped Unicode), no NaN, and the declared tuple order. Only
observed_at and digest are excluded. A supplied digest is checked, so corrupted
wire payloads cannot claim an identity.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ISO_Z_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
PR_STATE_SCHEMA_V2: Final = 2


class EnumPrState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


class ModelPrStateEmitRequest(BaseModel):
    # RuntimeLocal validates JSON-shaped Python dictionaries. Accept wire enum
    # strings and arrays here, while keeping scalar facts strict. The event
    # subclass additionally enforces strict enum/tuple construction in Python.
    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    pr_number: int = Field(gt=0, strict=True)
    state: EnumPrState
    head_sha: str
    base: str
    head_ref: str
    title: str = Field(max_length=200)
    draft: bool = Field(strict=True)
    author: str
    author_is_bot: bool = Field(strict=True)
    labels: tuple[str, ...]
    armed: bool = Field(strict=True)
    queued: bool = Field(strict=True)
    watcher_class: str
    ci_verdict: Literal["GREEN", "RED", "PENDING", "NONE"]
    red_contexts: tuple[str, ...]
    pending_contexts: tuple[str, ...]
    ci_read_at: str
    merged_at: str
    observed_at: str = Field(pattern=ISO_Z_PATTERN)

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: str) -> str:
        datetime.fromisoformat(value)  # Reject impossible calendar dates too.
        return value


class ModelPrStateObservedEvent(ModelPrStateEmitRequest):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    digest: str = Field(default="", pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        canonical = json.dumps(
            self.model_dump(mode="json", exclude={"observed_at", "digest"}),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if self.digest and self.digest != digest:
            raise ValueError("digest does not match canonical PR state")
        object.__setattr__(self, "digest", digest)
        return self


class ModelPrCheckFact(BaseModel):
    """One check's newest completed copy: what a red-CI consumer needs without a GitHub read.

    ``run_id`` and ``workflow`` name the GitHub Actions workflow run the check ran in. A check
    posted by another app has no workflow run, so both are empty there (``run_id`` 0).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    check: str = Field(min_length=1)
    conclusion: str = Field(min_length=1)
    run_id: int = Field(ge=0)
    workflow: str
    completed_at: str = Field(pattern=ISO_Z_PATTERN)

    @model_validator(mode="after")
    def run_and_workflow_come_together(self) -> Self:
        if (self.run_id == 0) != (self.workflow == ""):
            raise ValueError("run_id and workflow must both be set or both be empty")
        datetime.fromisoformat(self.completed_at)
        return self


class ModelPrStateCheckFacts(BaseModel):
    """Schema version 2 additions, shared by the emit request and the observed event.

    ``checks`` holds the facts of the head's red contexts. ``base_red_checks`` names the checks
    that are red on the base branch's tip as the watcher last read it, and ``base_read`` says
    whether it read the tip at all (an unread base is not a green one).
    """

    schema_version: Literal[2] = PR_STATE_SCHEMA_V2
    checks: tuple[ModelPrCheckFact, ...]
    base_red_checks: tuple[str, ...]
    base_read: bool = Field(strict=True)

    @model_validator(mode="after")
    def checks_name_red_contexts_once(self) -> Self:
        red = getattr(self, "red_contexts", ())
        names = [fact.check for fact in self.checks]
        if len(names) != len(set(names)):
            raise ValueError("checks must name each check once")
        if not set(names) <= set(red):
            raise ValueError("checks must name red_contexts only")
        if self.base_red_checks != tuple(sorted(set(self.base_red_checks))):
            raise ValueError("base_red_checks must be sorted and unique")
        if self.base_red_checks and not self.base_read:
            raise ValueError("base_red_checks without base_read")
        return self


class ModelPrStateEmitRequestV2(ModelPrStateEmitRequest, ModelPrStateCheckFacts):
    pass


class ModelPrStateObservedEventV2(ModelPrStateObservedEvent, ModelPrStateCheckFacts):
    pass


PR_STATE_V2_FIELDS = frozenset(ModelPrStateCheckFacts.model_fields)


def pr_state_event_from_wire(
    value: Mapping[str, object],
) -> ModelPrStateObservedEvent:
    """Type a watcher wire payload of either schema version, ignoring transport enrichment.

    A payload without ``schema_version`` is version 1. The digest is required and checked.
    """
    event_cls: type[ModelPrStateObservedEvent] = (
        ModelPrStateObservedEventV2
        if value.get("schema_version") == PR_STATE_SCHEMA_V2
        else ModelPrStateObservedEvent
    )
    payload = {k: value[k] for k in event_cls.model_fields if k in value}
    if "digest" not in payload:
        raise ValueError("wire observation must carry digest")
    return event_cls.model_validate_json(json.dumps(payload))
