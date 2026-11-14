# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire types of the public-consumer check (OMN-23145, rebuild plan section 5)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumScopeKind(StrEnum):
    PUBLIC_REPOSITORY = "public_repository"
    PUBLISHED_PACKAGE = "published_package"


class EnumRarePathCategory(StrEnum):
    RECOVERY = "recovery"
    ROLLBACK = "rollback"
    ON_CALL = "on_call"


class EnumEntityDisposition(StrEnum):
    #: Nothing public reaches the entity (or every consumer is resolved): the next stage may proceed.
    CLEARED = "cleared"
    #: At least one public consumer has no recorded resolution.
    KEPT_UNRESOLVED_CONSUMER = "kept_unresolved_consumer"
    #: A scope could not be read, so absence of a consumer proves nothing.
    KEPT_SEARCH_UNREAD = "kept_search_unread"
    #: A declared rare path that names its drill.
    KEPT_RARE_PATH = "kept_rare_path"
    #: A public scope names the archived repository the batch comes from, and the mention has no recorded resolution.
    KEPT_ARCHIVED_REPOSITORY_MENTION = "kept_archived_repository_mention"


class EnumBatchDecision(StrEnum):
    ACCEPTED = "accepted"
    REFUSED = "refused"


class ModelSearchScope(BaseModel):
    """One public repository or published package the check searches."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumScopeKind
    name: str = Field(min_length=1)


class ModelRarePathDeclaration(BaseModel):
    """An entity declared a rare path; it stays only when ``drill`` names a drill."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: EnumRarePathCategory
    drill: str = ""


class ModelEntity(BaseModel):
    """One entity proposed for removal, with the references a consumer would use."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_id: str = Field(min_length=1)
    #: The reference strings searched for: dotted module name, file path, skill or node name.
    references: tuple[str, ...] = Field(min_length=1)
    rare_path: ModelRarePathDeclaration | None = None

    @model_validator(mode="after")
    def _references_are_not_blank(self) -> ModelEntity:
        if any(not reference.strip() for reference in self.references):
            raise ValueError("a reference is blank")
        return self


class ModelConsumerResolution(BaseModel):
    """A consumer that has been dealt with, citing the change that did it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: ModelSearchScope
    locator: str = Field(min_length=1)
    resolution_ref: str = Field(min_length=1)


class ModelPublicConsumerCheckRequest(BaseModel):
    """A removal batch: one repository, the entities proposed for removal, known resolutions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    entities: tuple[ModelEntity, ...] = Field(min_length=1)
    resolutions: tuple[ModelConsumerResolution, ...] = ()
    #: The names the batch's archived repository is mentioned by (empty when the repository is not archived). A public
    #: mention of any of them keeps every entity of the batch until the mention has a recorded resolution.
    archived_repository_names: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _archived_names_are_not_blank(self) -> ModelPublicConsumerCheckRequest:
        if any(not name.strip() for name in self.archived_repository_names):
            raise ValueError("an archived repository name is blank")
        return self

    @model_validator(mode="after")
    def _entities_are_distinct(self) -> ModelPublicConsumerCheckRequest:
        ids = [entity.entity_id for entity in self.entities]
        if len(ids) != len(set(ids)):
            raise ValueError("an entity is named twice in the batch")
        return self


class ModelSourceHit(BaseModel):
    """What a source returns: one reference found at one place in one scope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    locator: str = Field(min_length=1)
    line: int = Field(ge=0)
    matched_reference: str = Field(min_length=1)


class ModelConsumerHit(BaseModel):
    """A hit as recorded in the evidence, with the scope it was found in."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: ModelSearchScope
    locator: str
    line: int
    matched_reference: str
    #: A hit in the entity's own repository is the graph stage's business, not a public consumer.
    own_repository: bool
    resolved: bool
    resolution_ref: str = ""


class ModelArchivedMention(BaseModel):
    """A public mention of the batch's archived repository, with the scope it was found in."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: ModelSearchScope
    locator: str
    line: int
    matched_name: str
    resolved: bool
    resolution_ref: str = ""


class ModelScopeReading(BaseModel):
    """Whether one scope was read for one entity, and what it held."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: ModelSearchScope
    read: bool
    hit_count: int = Field(ge=0)
    #: Why the scope was not read; empty when it was.
    unread_reason: str = ""


class ModelEntityEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scopes: tuple[ModelScopeReading, ...]
    hits: tuple[ModelConsumerHit, ...]


class ModelEntityVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_id: str
    disposition: EnumEntityDisposition
    kept: bool
    reasons: tuple[str, ...]
    evidence: ModelEntityEvidence


class ModelPublicConsumerCheckResult(BaseModel):
    """The batch verdict: refused when any entity it names is kept."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: str
    repository: str
    decision: EnumBatchDecision
    kept_entity_ids: tuple[str, ...]
    verdicts: tuple[ModelEntityVerdict, ...]
    #: Every public mention of the batch's archived repository that was read, and why the batch keeps its entities.
    archived_mentions: tuple[ModelArchivedMention, ...] = ()
    reasons: tuple[str, ...] = ()


class ModelPublicConsumerCheckFailed(BaseModel):
    """Published when a request cannot be graded at all (an unreadable payload)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: str
    reason: str
    terminal_failure_cause: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _cause_is_the_reason(cls, data: object) -> object:
        """The runtime's failure-terminal guard reads ``terminal_failure_cause``."""
        if isinstance(data, dict) and data.get("terminal_failure_cause") is None:
            return {**data, "terminal_failure_cause": data.get("reason")}
        return data
