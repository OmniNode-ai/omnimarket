# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed settings, requests, records and results of the code quarantine node (OMN-23144)."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ID = r"^[A-Za-z0-9._:/-]+$"


def _relative(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not value.strip():
        raise ValueError("a path must be relative to the repository root, with no '..'")
    return value


class EnumQuarantineMode(StrEnum):
    QUARANTINE = "quarantine"
    CHECK = "check"
    REMOVE = "remove"


class EnumQuarantineBackend(StrEnum):
    IN_MEMORY = "in-memory"
    FILES = "files"


class EnumQuarantineOutcome(StrEnum):
    QUARANTINED = "quarantined"
    STILL_QUARANTINED = "still-quarantined"
    DUE_FOR_REMOVAL = "due-for-removal"
    RESTORED = "restored"
    REMOVED = "removed"
    REFUSED_NO_EVIDENCE = "refused-no-evidence"
    REFUSED_PUBLIC_CONSUMER = "refused-public-consumer"
    REFUSED_UNKNOWN_PATH = "refused-unknown-path"
    REFUSED_ALREADY_QUARANTINED = "refused-already-quarantined"
    REFUSED_NOT_QUARANTINED = "refused-not-quarantined"
    REFUSED_CLOCK_RUNNING = "refused-clock-running"
    REFUSED_NO_TAG = "refused-no-tag"
    REFUSED_NO_CATALOG_ROW = "refused-no-catalog-row"
    REFUSED_TESTS_NOT_WITH_MODULE = "refused-tests-not-with-module"
    REFUSED_RARE_PATH = "refused-rare-path"
    BACKEND_FAILED = "backend-failed"


#: The outcomes that are the thing asked for, or the honest answer to a check. A restore is not one: the quarantine
#: did not hold, so it reads NOT WORKING (and exits 1) while the entity is safely back in the build.
OK_OUTCOMES = frozenset(
    {
        EnumQuarantineOutcome.QUARANTINED,
        EnumQuarantineOutcome.STILL_QUARANTINED,
        EnumQuarantineOutcome.DUE_FOR_REMOVAL,
        EnumQuarantineOutcome.REMOVED,
    }
)


class EnumEntityKind(StrEnum):
    """What the entity is. Stage 4 of the unused-code removal (OMN-23149) names two kinds that need one more read."""

    #: Not in stage 4: only the reads every quarantine already needs.
    UNSTAGED = "unstaged"
    #: A module whose only references are tests: it leaves with those tests, never the tests alone.
    TEST_ONLY_MODULE = "test-only-module"
    #: A skill never seen in use: it leaves only after a rare-path check names no drill that uses it.
    SKILL_NEVER_SEEN = "skill-never-seen"


class EnumEntityState(StrEnum):
    QUARANTINED = "quarantined"
    RESTORED = "restored"
    REMOVED = "removed"


class EnumFailedGate(StrEnum):
    CLEAN_BUILD = "clean-build"
    LAB_RUNTIME_START = "lab-runtime-start"
    MISSING_READING = "missing-reading"


class ModelPolicy(BaseModel):
    """The two numbers of section 5: how long an entity stays out, and the observation it needs first."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    quarantine_days: int = Field(ge=1)
    min_observation_days: int = Field(ge=1)


class ModelRemovalEvidence(BaseModel):
    """What must be read before an entity may leave the build: the observation and the public-consumer search."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_days: int = Field(ge=0)
    #: Where the observation and the public-consumer search are recorded (a ledger row, a report path).
    evidence_ref: str
    public_consumers: int = Field(ge=0)


class ModelRarePathCheck(BaseModel):
    """The rare-path declaration read for a skill: the drills that were read, and those of them that use the skill."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Where the check is recorded (a ledger row, a report path).
    evidence_ref: str
    #: Every drill the check read. A check that read none is no check.
    drills_checked: tuple[str, ...]
    #: The drills among them that use the skill. Any one keeps the skill.
    drills_using: tuple[str, ...] = ()


class ModelCatalogRow(BaseModel):
    """One catalog row of a removal: the entity, the tag of its batch, the sha the tag names, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity: str = Field(pattern=_ID)
    #: Not shape-checked here: a row whose tag or sha does not match the batch's tag is refused by the handler.
    tag: str
    sha: str
    reason: str


class ModelRemovalBatch(BaseModel):
    """One repository per batch: one tag, and a catalog row for every entity in it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tag: str
    entities: tuple[str, ...] = Field(min_length=1)
    catalog_rows: tuple[ModelCatalogRow, ...]


class ModelQuarantineRecord(BaseModel):
    """The durable state of one entity: where its files are, when the clock started, and where it stands."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    entity: str
    state: EnumEntityState
    entity_paths: tuple[str, ...]
    test_paths: tuple[str, ...]
    #: None while the entity is in the build (restored) or gone (removed).
    clock_started_at: datetime | None = None
    restored_reason: str = ""
    restarts: int = Field(default=0, ge=0)
    #: A record written before stage 4 carries no kind and reads as unstaged.
    entity_kind: EnumEntityKind = EnumEntityKind.UNSTAGED


class ModelQuarantineRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9T_-]+$")
    mode: EnumQuarantineMode
    repository: str = Field(pattern=_ID)
    #: The entity for quarantine and check. A removal names its entities in the batch.
    entity: str = ""
    entity_paths: tuple[str, ...] = ()
    test_paths: tuple[str, ...] = ()
    evidence: ModelRemovalEvidence | None = None
    batch: ModelRemovalBatch | None = None
    #: Stage 4 (OMN-23149). The default reads as before: no further read beyond the observation and the consumers.
    entity_kind: EnumEntityKind = EnumEntityKind.UNSTAGED
    #: The tests that reference a test-only module; every one of them must be among ``test_paths``.
    test_references: tuple[str, ...] = ()
    #: The rare-path check of a skill never seen.
    rare_path: ModelRarePathCheck | None = None

    @field_validator("entity_paths", "test_paths", "test_references")
    @classmethod
    def _paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _relative(item)
        return value

    @model_validator(mode="after")
    def _shape(self) -> ModelQuarantineRequest:
        if self.mode is EnumQuarantineMode.QUARANTINE and not (
            self.entity and self.entity_paths and self.evidence
        ):
            raise ValueError(
                "a quarantine names the entity, its paths and its evidence"
            )
        if self.mode is EnumQuarantineMode.CHECK and not self.entity:
            raise ValueError("a check names the entity")
        if self.mode is EnumQuarantineMode.REMOVE and not self.batch:
            raise ValueError("a removal carries its batch")
        return self


class ModelQuarantineEvent(BaseModel):
    """One typed event of a run, reported once. ``correlation_id`` is the run id, so a chain reads in order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    kind: str
    repository: str
    entity: str
    outcome: EnumQuarantineOutcome
    detail: str
    reading: str


class ModelQuarantineResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    mode: EnumQuarantineMode
    repository: str
    entity: str
    outcome: EnumQuarantineOutcome
    detail: str
    failed_gate: EnumFailedGate | None = None
    #: Paths out of the build after the run for this repository.
    excluded_paths: tuple[str, ...] = ()
    #: Entities taken away by a removal.
    removed_entities: tuple[str, ...] = ()
    clock_started_at: datetime | None = None
    changed: bool = False

    @property
    def ok(self) -> bool:
        return self.outcome in OK_OUTCOMES

    @property
    def reading(self) -> str:
        return "WORKING" if self.ok else "NOT WORKING"


class ModelInMemoryBackend(BaseModel):
    """The in-memory default: the files of each repository, the tags, and what each gate reads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    files: dict[str, tuple[str, ...]]
    tags: dict[str, dict[str, str]]
    clean_build_green: bool
    lab_runtime_start_green: bool
    reading_present: bool


class ModelGateCommand(BaseModel):
    """A gate command: argv run without a shell in the repository root; exit 0 is green."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    argv: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: int = Field(ge=1)


class ModelFilesBackend(BaseModel):
    """The real backend: a state file and a catalog file, repository roots, and the three gates as commands."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state_file: str
    catalog_file: str
    repository_roots: dict[str, str]
    git_binary: str = Field(min_length=1)
    git_timeout_seconds: int = Field(ge=1)
    clean_build: ModelGateCommand
    lab_runtime_start: ModelGateCommand
    reading: ModelGateCommand

    @field_validator("state_file", "catalog_file")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not PurePosixPath(value).is_absolute():
            raise ValueError("a state or catalog file must be an absolute path")
        return value


class ModelQuarantineReport(BaseModel):
    """How one event is reported: a command whose last argument is the row text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    append_argv: tuple[str, ...] = Field(min_length=1)
    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=1)
    environment: dict[str, str]


class ModelQuarantineOverlay(BaseModel):
    """Every deployment fact of the node. No key has a default.

    ``backend`` names the seam the node runs on: ``in-memory`` (needs ``in_memory``) or ``files`` (needs ``files``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: EnumQuarantineBackend
    policy: ModelPolicy
    files: ModelFilesBackend | None = None
    in_memory: ModelInMemoryBackend | None = None
    report: ModelQuarantineReport

    @model_validator(mode="after")
    def _one_backend_block(self) -> ModelQuarantineOverlay:
        if self.backend is EnumQuarantineBackend.FILES and (
            self.files is None or self.in_memory
        ):
            raise ValueError(
                "backend files needs the files block and no in_memory block"
            )
        if self.backend is EnumQuarantineBackend.IN_MEMORY and (
            self.in_memory is None or self.files
        ):
            raise ValueError(
                "backend in-memory needs the in_memory block and no files block"
            )
        return self


_SPACE = re.compile(r"\s*\|\s*|\s+")


def one_line(text: str, limit: int = 240) -> str:
    return _SPACE.sub(" ", text).strip()[:limit]
