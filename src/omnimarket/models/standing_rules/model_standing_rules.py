# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dispatch admission is a typed decision over one caller-read snapshot."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


Placement = Literal["auto", "lab", "cloud"]
DispatchKind = Literal["agent", "workflow", "runner", "lab"]


class ModelStandingRuleSource(_Model):
    stamp: AwareDatetime
    lane: str = Field(min_length=1)


class ModelStandingRuleFactExpiry(_Model):
    kind: Literal["fact"]
    fact: str = Field(min_length=1)


class ModelStandingRuleTimeExpiry(_Model):
    kind: Literal["time"]
    at: AwareDatetime


class ModelStandingRule(_Model):
    id: str = Field(min_length=1)
    source_ruling: ModelStandingRuleSource
    dispatch_kinds: tuple[DispatchKind, ...] = Field(min_length=1)
    check: Literal["cloud_first"]
    expires_from: Annotated[
        ModelStandingRuleFactExpiry | ModelStandingRuleTimeExpiry,
        Field(discriminator="kind"),
    ]
    mode: Literal["refuse", "route"]
    models: tuple[str, ...] = Field(min_length=1)
    credit_fact: str = Field(min_length=1)
    slot_fact: str = Field(min_length=1)
    order: tuple[Literal["cloud", "lab"], ...] = Field(min_length=1)
    fix: str = Field(min_length=1)


class ModelStandingRuleFactBinding(_Model):
    path: str = Field(min_length=1)
    read: str = Field(min_length=1)


class ModelStandingRulesRegistry(_Model):
    rules: tuple[ModelStandingRule, ...] = Field(min_length=1)
    ledger: ModelStandingRuleFactBinding
    audit_path: str = Field(min_length=1)
    facts: dict[str, ModelStandingRuleFactBinding]


class ModelStandingRuleFact(_Model):
    remaining: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    available: bool | None = None
    expires_at: AwareDatetime | None = None


class ModelStandingRulesDispatch(_Model):
    request_id: str = Field(min_length=1)
    lane: str = Field(min_length=1)
    dispatch_kind: DispatchKind
    model: str = Field(min_length=1)
    repo_only: bool
    lab_resources: tuple[str, ...] = ()
    placement: Placement


class ModelStandingRulesSnapshot(_Model):
    dispatch: ModelStandingRulesDispatch
    now: AwareDatetime
    registry: ModelStandingRulesRegistry
    rulings: tuple[ModelStandingRuleSource, ...]
    facts: dict[str, ModelStandingRuleFact]


class ModelStandingRulesRoute(_Model):
    row: ModelStandingRulesDispatch
    order: tuple[Literal["cloud", "lab"], ...]


class ModelStandingRulesResult(_Model):
    request_id: str
    verdict: Literal["allow", "refuse", "route", "UNREAD"]
    rule: str = ""
    fix: str = ""
    route: ModelStandingRulesRoute | None = None
    lapsed: tuple[str, ...] = ()


class ModelStandingRulesRequest(_Model):
    dispatch: ModelStandingRulesDispatch
    now: AwareDatetime | None = None


class ModelStandingRulesAudit(_Model):
    at: datetime
    kind: Literal["LAPSED", "REFUSED", "UNREAD"]
    rule: str
    request_id: str
    fix: str = ""
