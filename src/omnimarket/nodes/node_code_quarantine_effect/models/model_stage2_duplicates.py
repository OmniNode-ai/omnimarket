# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed requests, readings and results of stage 2 of unused-code removal: duplicated node contract names (OMN-23147)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_code_quarantine_effect.models.model_code_quarantine import (
    _ID,
    EnumQuarantineOutcome,
    ModelRemovalEvidence,
    _relative,
)


class EnumStage2Mode(StrEnum):
    QUARANTINE = "quarantine"
    REMOVE = "remove"


class EnumStage2Disposition(StrEnum):
    #: The loaded copy was read and kept; every other copy went through the quarantine handler (or its removal).
    LOADED_COPY_KEPT = "loaded-copy-kept"
    #: The loaded copy could not be read, so every copy stays and the group is reported as unread.
    KEPT_LOADED_COPY_UNREAD = "kept-loaded-copy-unread"
    #: The reading is clean but dev loads none of the copies named here; nothing proves which is spare.
    KEPT_NO_LOADED_COPY = "kept-no-loaded-copy"
    #: Fewer than two copies are named, so there is no duplicate.
    KEPT_NOT_DUPLICATED = "kept-not-duplicated"


#: The dispositions that stand for a read the node could not finish; each is reported once as a typed event.
KEPT_DISPOSITIONS = frozenset(
    {
        EnumStage2Disposition.KEPT_LOADED_COPY_UNREAD,
        EnumStage2Disposition.KEPT_NO_LOADED_COPY,
        EnumStage2Disposition.KEPT_NOT_DUPLICATED,
    }
)


class ModelDuplicateCopy(BaseModel):
    """One copy of a contract name: where it lives, its files and tests, and what is known of its use."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(pattern=_ID)
    entity_paths: tuple[str, ...] = Field(min_length=1)
    test_paths: tuple[str, ...] = ()
    evidence: ModelRemovalEvidence

    @model_validator(mode="after")
    def _relative_paths(self) -> ModelDuplicateCopy:
        for item in (*self.entity_paths, *self.test_paths):
            _relative(item)
        return self


class ModelDuplicateGroup(BaseModel):
    """A node contract name found in more than one repository."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_name: str = Field(pattern=_ID)
    copies: tuple[ModelDuplicateCopy, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _one_copy_per_repository(self) -> ModelDuplicateGroup:
        repositories = [copy.repository for copy in self.copies]
        if len(repositories) != len(set(repositories)):
            raise ValueError("a repository holds one copy of a contract name")
        return self


class ModelRepositoryTag(BaseModel):
    """The tag of one repository's removal batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(pattern=_ID)
    tag: str = Field(min_length=1)


class ModelStage2Request(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9T_-]+$")
    mode: EnumStage2Mode
    groups: tuple[ModelDuplicateGroup, ...] = Field(min_length=1)
    #: A removal names the tag of each repository's batch; one repository per batch.
    tags: tuple[ModelRepositoryTag, ...] = ()

    @model_validator(mode="after")
    def _names_are_distinct(self) -> ModelStage2Request:
        names = [group.contract_name for group in self.groups]
        if len(names) != len(set(names)):
            raise ValueError("a contract name is named twice in the request")
        if self.mode is EnumStage2Mode.REMOVE and not self.tags:
            raise ValueError("a removal names the tag of each repository's batch")
        return self


class ModelStage2CopyOutcome(BaseModel):
    """What happened to one copy: kept as the loaded copy, or the outcome of its quarantine or removal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    kept: bool
    #: None for the loaded copy and for every copy of a group that was kept whole.
    outcome: EnumQuarantineOutcome | None = None
    detail: str = ""


class ModelStage2GroupResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_name: str
    disposition: EnumStage2Disposition
    loaded_repository: str = ""
    detail: str
    copies: tuple[ModelStage2CopyOutcome, ...]


class ModelStage2Event(BaseModel):
    """One typed event of a group kept whole, reported once; ``correlation_id`` is the run id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    kind: str
    contract_name: str
    disposition: EnumStage2Disposition
    detail: str
    reading: str


class ModelStage2Result(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    mode: EnumStage2Mode
    groups: tuple[ModelStage2GroupResult, ...]

    @property
    def unread(self) -> tuple[str, ...]:
        return tuple(
            g.contract_name
            for g in self.groups
            if g.disposition is EnumStage2Disposition.KEPT_LOADED_COPY_UNREAD
        )

    @property
    def ok(self) -> bool:
        """A group kept whole is an honest answer; a restore or a failed step in a copy is not."""
        bad = {EnumQuarantineOutcome.RESTORED, EnumQuarantineOutcome.BACKEND_FAILED}
        return not any(
            copy.outcome in bad for group in self.groups for copy in group.copies
        )

    @property
    def reading(self) -> str:
        return "WORKING" if self.ok else "NOT WORKING"
