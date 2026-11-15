# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the merged-change reading: the facts a reader supplies, the declared rule config, the readings.

One reading answers, for one merged change: is it in the build, is the node it changes loaded, when did that
node last run and what did it answer, and when it is not running, the one step that is missing.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("a timestamp must carry a timezone")
    return value


def _aware_optional(value: datetime | None) -> datetime | None:
    return None if value is None else _aware(value)


class ModelMcrRequirement(BaseModel):
    """One thing a change needs before it counts as running, the code reported when it is missing, and what to do."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    requirement: str = Field(min_length=1)
    missing_step: str = Field(min_length=1)
    next_action: str = Field(min_length=1)


class ModelMcrRunKind(BaseModel):
    """How a change runs (schedule, topic, route, unit, overlay, library) and the requirements checked, in order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1)
    requires: tuple[str, ...] = Field(min_length=1)


class ModelMcrConfig(BaseModel):
    """Every verdict word, step code and rule order of the reading, declared by the contract or an overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_id: str = Field(min_length=1)
    verdict_running: str = Field(min_length=1)
    verdict_not_running: str = Field(min_length=1)
    verdict_unread: str = Field(min_length=1)
    no_runner_step: str = Field(min_length=1)
    no_runner_next_action: str = Field(min_length=1)
    requirements: tuple[ModelMcrRequirement, ...] = Field(min_length=1)
    run_kinds: tuple[ModelMcrRunKind, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> ModelMcrConfig:
        names = [kind.kind for kind in self.run_kinds]
        if len(set(names)) != len(names):
            raise ValueError("run_kinds repeats a kind")
        known = {item.requirement for item in self.requirements}
        if len(known) != len(self.requirements):
            raise ValueError("requirements repeats a requirement")
        for kind in self.run_kinds:
            missing = [name for name in kind.requires if name not in known]
            if missing:
                raise ValueError(
                    f"run kind {kind.kind!r} requires undeclared {missing[0]!r}"
                )
        return self


class ModelMcrChange(BaseModel):
    """One merged change to read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    number: int = Field(gt=0)
    merge_sha: str = ""


class ModelMcrRunner(BaseModel):
    """The declared map from a change to the node or unit it changes and how it runs. It comes from an overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    number: int = Field(gt=0)
    kind: str = Field(min_length=1)
    nodes: tuple[str, ...] = ()
    trigger_topics: tuple[str, ...] = ()
    result_topics: tuple[str, ...] = ()
    # Topics the node publishes on that other nodes publish on too: the runtime's log names no publisher, so a
    # publish there is not evidence that this node ran. With no result topic of its own the "ran" requirement is unread.
    shared_result_topics: tuple[str, ...] = ()
    unit: str = ""
    # True only when the declaring reader verified that nothing calls the node: a change with no result evidence
    # then reads not running (the missing step is a caller), where without it the run evidence is simply undeclared.
    no_caller: bool = False
    # The lab host that runs the unit (a name the overlay's unit hosts table resolves); empty for a node.
    host: str = ""


class ModelMcrBuildFact(BaseModel):
    """Whether the running system holds one change, and which reader said so."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    number: int = Field(gt=0)
    in_build: bool
    source: str = Field(min_length=1)


class ModelMcrLoadedNode(BaseModel):
    """A node the running system discovered."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str = Field(min_length=1)
    loaded_at: datetime | None = None

    _utc = field_validator("loaded_at")(_aware_optional)


class ModelMcrSubscription(BaseModel):
    """A consumer of a topic that joined for a node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    joined_at: datetime

    _utc = field_validator("joined_at")(_aware)


class ModelMcrPublished(BaseModel):
    """The last output event the running system published on a topic, and how many it published."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(min_length=1)
    last_at: datetime
    count: int = Field(ge=1)

    _utc = field_validator("last_at")(_aware)


class ModelMcrUnitRun(BaseModel):
    """The last run of a host unit and its result as the unit's own log states it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit: str = Field(min_length=1)
    last_run_at: datetime
    result: str = Field(min_length=1)
    # False when the unit's own state says the run failed (the reader decides by the effect contract's declared word).
    ok: bool = True

    _utc = field_validator("last_run_at")(_aware)


class ModelMcrRuntimeFacts(BaseModel):
    """What the runtime readers returned. ``unread`` names each fact group a reader could not read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    loaded_nodes: tuple[ModelMcrLoadedNode, ...] = ()
    subscriptions: tuple[ModelMcrSubscription, ...] = ()
    published: tuple[ModelMcrPublished, ...] = ()
    unit_runs: tuple[ModelMcrUnitRun, ...] = ()
    unread: tuple[str, ...] = ()


class ModelMcrRequest(BaseModel):
    """Everything one run reads. The node has no clock: ``now`` is a fact the caller supplies."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: datetime
    config: ModelMcrConfig
    build_started_at: datetime | None = None
    changes: tuple[ModelMcrChange, ...]
    runners: tuple[ModelMcrRunner, ...] = ()
    build_facts: tuple[ModelMcrBuildFact, ...] = ()
    # The repositories whose build facts a reader could not read: a change of one with no build fact reads unread.
    build_unread: tuple[str, ...] = ()
    runtime: ModelMcrRuntimeFacts = ModelMcrRuntimeFacts()

    _utc = field_validator("now")(_aware)
    _utc_build = field_validator("build_started_at")(_aware_optional)


class ModelMcrNodeReading(BaseModel):
    """One node of a change as the runtime facts read it. None means the fact group was unread."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str
    loaded: bool | None
    subscribed_topics: tuple[str, ...]
    last_result_topic: str
    last_run_at: datetime | None


class ModelMcrReading(BaseModel):
    """One change's reading: in build or not, node loaded, last run, last result, and the one missing step."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    number: int
    merge_sha: str
    kind: str
    in_build: bool | None
    nodes: tuple[ModelMcrNodeReading, ...]
    last_run_at: datetime | None
    last_result: str
    verdict: str
    missing_step: str
    next_action: str
    detail: str


class ModelMcrSummary(BaseModel):
    """Verdict counts of one run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total: int
    running: int
    not_running: int
    unread: int


class ModelMcrResult(BaseModel):
    """One reading per change, in the order the changes were given."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: datetime
    build_started_at: datetime | None
    readings: tuple[ModelMcrReading, ...]
    summary: ModelMcrSummary
