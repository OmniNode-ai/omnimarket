# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the code graph lookup and its versions (OMN-23648).

A version is what the graph knew of one repository at one commit: where each class and function is
defined, which file imports which, the field shape of each model, and the nodes by archetype. A
lookup asks one of five questions of one version (the head, or the one stored for a commit) and
answers from that version only: a commit with no stored version is a typed not-found that names the
nearest stored version, never the head's answer.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_code_graph import ModelFileReferences


class EnumCodeGraphQuestion(StrEnum):
    """The six questions a lookup answers."""

    #: Where a symbol (class or function) is defined.
    DEFINITION = "definition"
    #: Which files import a module file.
    IMPORTERS = "importers"
    #: Which files a change to the named files reaches (their importers, transitively).
    REACH = "reach"
    #: The nearest exemplar node of an archetype.
    EXEMPLAR = "exemplar"
    #: Whether a model of the same field shape exists, whatever its name.
    SAME_SHAPE = "same-shape"
    #: Which files still use a removed Python symbol or module, from the imports as written.
    USERS = "users"
    #: Where a node lives: its directory, contract, metadata, overlay and the workflows that name it.
    LOCATE = "locate"


class EnumRemovalKind(StrEnum):
    """What a removal took out of a repository."""

    #: A class, function or other name a module (or a package ``__init__``) bound.
    PYTHON_SYMBOL = "python_symbol"
    #: A module file or package.
    PYTHON_MODULE = "python_module"


class EnumReferenceForm(StrEnum):
    """How a file refers to a removed name."""

    DIRECT_IMPORT = "direct-import"
    ATTRIBUTE_USE = "attribute-use"
    #: Imported by a package ``__init__``, by ``import x as x``, or listed in the file's ``__all__``.
    RE_EXPORT = "re-export"
    STAR_IMPORT = "star-import"
    IMPORT_MODULE_LITERAL = "import-module-literal"


class EnumLookupFault(StrEnum):
    """A break injected on purpose, so a test can show its assertion turn red."""

    NONE = "none"
    #: Match shapes by class name instead of by field shape.
    MATCH_SHAPES_BY_NAME = "match-shapes-by-name"
    #: Answer from the head when asked as of a commit.
    ANSWER_FROM_HEAD = "answer-from-head"


class EnumLookupServedFrom(StrEnum):
    """Which store a lookup was answered from."""

    #: The Memgraph instance the architecture graph overlay names, queried by Cypher.
    MEMGRAPH = "memgraph"
    #: The file-backed store, built from the repository clones; only when Memgraph is unreachable.
    FILE_STORE = "file-store"


class EnumLookupFallbackReason(StrEnum):
    """Why a lookup left Memgraph for the file store: the typed reason a fallback answer carries."""

    #: ARCH_GRAPH_BOLT_URI is unset and no overlay row names this host's Bolt address.
    BOLT_URI_UNRESOLVED = "bolt-uri-unresolved"
    #: The Bolt port refused the connection.
    CONNECTION_REFUSED = "connection-refused"
    #: Memgraph accepted the connection and did not answer in time.
    READ_TIMED_OUT = "read-timed-out"
    #: Any other transport failure (no route, reset, protocol handshake lost).
    UNREACHABLE = "unreachable"
    #: The question reads the imports as written, which Memgraph does not hold yet.
    QUESTION_NOT_IN_MEMGRAPH = "question-not-in-memgraph"


class EnumNodeFileKind(StrEnum):
    """The kind of a file that belongs to a node (OMN-26202)."""

    DIRECTORY = "directory"
    CONTRACT = "contract"
    METADATA = "metadata"
    OVERLAY = "overlay"
    WORKFLOW = "workflow"


class ModelNodeFile(BaseModel):
    """One canonical repository-relative path that belongs to one node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The node's directory name (``node_code_graph_effect``).
    node: str = Field(min_length=1)
    kind: EnumNodeFileKind
    path: str = Field(min_length=1)


class ModelSymbolDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    kind: Literal["class", "function"]
    path: str = Field(min_length=1)
    line: int = Field(ge=1)


class ModelShapeEntry(BaseModel):
    """The field shape of one class: its annotated fields, hashed in sorted order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    line: int = Field(ge=1)
    #: sha256 of the sorted ``name: annotation`` fields; the class name is not part of it.
    signature: str = Field(min_length=12)
    fields: tuple[str, ...] = Field(min_length=1)


class ModelExemplarNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Directory of the node, repository relative.
    path: str = Field(min_length=1)
    name: str = Field(min_length=1)
    archetype: str = Field(min_length=1)


class ModelImportRelation(BaseModel):
    """One file imports another, both in the same repository."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    importer_path: str = Field(min_length=1)
    imported_path: str = Field(min_length=1)


class ModelCodeGraphVersion(BaseModel):
    """What the graph knew of one repository at one commit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    commit: str = Field(min_length=1)
    #: The commit's own time, when the clone could say; it orders versions and names the nearest one.
    committed_at: datetime | None = None
    definitions: tuple[ModelSymbolDefinition, ...] = ()
    shapes: tuple[ModelShapeEntry, ...] = ()
    imports: tuple[ModelImportRelation, ...] = ()
    exemplars: tuple[ModelExemplarNode, ...] = ()
    #: Each file's imports as written, which a ``users`` lookup matches against a dotted identity.
    references: tuple[ModelFileReferences, ...] = ()
    #: Where each node lives and which non-Python files belong to it (OMN-26202).
    node_files: tuple[ModelNodeFile, ...] = ()


class ModelCodeGraphLookupSettings(BaseModel):
    """The overlay's lookup block: how many versions of a repository are kept."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    retention_versions: int = Field(ge=1)


class ModelCodeGraphLookupCommand(BaseModel):
    """Ask one question of one repository's graph, at its head or as of a commit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    question: EnumCodeGraphQuestion
    repo: str = Field(min_length=1)
    #: DEFINITION and SAME_SHAPE: the class or function name.
    symbol: str | None = None
    #: IMPORTERS: the repository-relative path of the imported file.
    path: str | None = None
    #: REACH: the repository-relative paths of the changed files.
    paths: tuple[str, ...] = ()
    #: EXEMPLAR: the archetype value (``effect``, ``compute``, ...).
    archetype: str | None = None
    #: EXEMPLAR: words that rank the nodes of the archetype (a node whose name holds one ranks first).
    hint: str | None = None
    #: USERS: what the removal took out, and the dotted identity of it (``a.mod.f``, or ``a.mod``).
    removal_kind: EnumRemovalKind | None = None
    identity: str | None = None
    #: USERS: the names ``from <module> import *`` exposed in the base revision (the statically known
    #: list of the removal); ``None`` means no list, and a star import then matches nothing.
    star_export_list: tuple[str, ...] | None = None
    #: USERS: the head revision's literal ``__all__`` still lists the removed name, so a star import
    #: fails on the head whether or not the file uses the name.
    head_all_lists_name: bool = False
    #: USERS: dotted submodules (``a.mod``) whose name the package binds to another object in the base
    #: or the head revision; ``from a import mod`` then does not reach the submodule.
    rebound_submodules: tuple[str, ...] = ()
    #: LOCATE: a node name, a contract path or a workflow path.
    target: str | None = None
    #: The commit to answer as of; absent means the head, the newest stored version.
    as_of: str | None = None
    fault: EnumLookupFault = EnumLookupFault.NONE

    @model_validator(mode="after")
    def _arguments(self) -> ModelCodeGraphLookupCommand:
        needed = {
            EnumCodeGraphQuestion.DEFINITION: bool(self.symbol),
            EnumCodeGraphQuestion.SAME_SHAPE: bool(self.symbol),
            EnumCodeGraphQuestion.IMPORTERS: bool(self.path),
            EnumCodeGraphQuestion.REACH: bool(self.paths),
            EnumCodeGraphQuestion.EXEMPLAR: bool(self.archetype),
            EnumCodeGraphQuestion.USERS: bool(self.identity)
            and self.removal_kind is not None,
            EnumCodeGraphQuestion.LOCATE: bool(self.target),
        }[self.question]
        if not needed:
            raise ValueError(f"the {self.question.value} question lacks its argument")
        return self


class ModelNearestVersion(BaseModel):
    """The stored version nearest an unstored commit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    commit: str
    committed_at: datetime | None = None
    #: ``by-commit-time`` when the clone could date the asked commit; ``unresolved`` when it could
    #: not, and the newest stored version is named instead.
    ordering: Literal["by-commit-time", "unresolved"]


class ModelLookupMatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    name: str | None = None
    line: int | None = None
    detail: str | None = None
    #: USERS: the reference form the file matched.
    reference_form: EnumReferenceForm | None = None


class ModelCodeGraphLookupResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    question: EnumCodeGraphQuestion
    repo: str
    #: ``found`` has at least one match; ``not-found`` is a clean negative answer, or a commit with
    #: no stored version (then ``nearest`` is set); ``failed`` is a lookup that could not be answered.
    status: Literal["found", "not-found", "failed"]
    #: The commit of the version the answer came from.
    answered_from: str | None = None
    matches: tuple[ModelLookupMatch, ...] = ()
    nearest: ModelNearestVersion | None = None
    reason: str | None = None
    #: The store that answered. A lookup that did not reach any store keeps the file-store default.
    served_from: EnumLookupServedFrom = EnumLookupServedFrom.FILE_STORE
    #: Set only on an answer served from the file store because Memgraph was unreachable.
    fallback_reason: EnumLookupFallbackReason | None = None
    #: The transport error text behind ``fallback_reason``.
    fallback_detail: str | None = None
