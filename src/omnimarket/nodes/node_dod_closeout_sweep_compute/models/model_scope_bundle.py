# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract models of the closeout sweep's scope files (OMN-26223).

Three input files and one output are JSON that the producer writes and a reader consumes:

``candidates``, ``pr-state`` and ``text-state`` (inputs of the ``bundle`` command) and the
scope bundle the command prints. Each has one model here. The producer validates its output
with the model before it writes anything; every reader parses with the same model and gets a
``ScopeContractError`` that names the file and the field, never an empty or zero result.
"""

from __future__ import annotations

import json
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

_STRICT = ConfigDict(extra="forbid", frozen=True, strict=True)


class ScopeContractError(ValueError):
    """A scope file that is not what its model reads: names the file and each failing field."""

    def __init__(self, file: str, fields: tuple[str, ...], detail: str) -> None:
        self.file = file
        self.fields = fields
        super().__init__(f"{file}: {detail}")


class ModelScopeCandidateRow(BaseModel):
    """One enumerated ticket: a row of the candidates file."""

    model_config = _STRICT
    id: str = Field(min_length=1)
    state: str = ""
    sources: list[str] = Field(default_factory=list)
    last_closeout_comment_at: str = ""
    title: str = ""


class ModelScopePrStateRow(BaseModel):
    """One ticket's PR state: a row of the pr-state file (the ``prs`` command's output)."""

    model_config = _STRICT
    id: str = Field(min_length=1)
    merged: list[str]
    open: list[str]
    error: str = ""


class ModelScopeTextStateRow(BaseModel):
    """One ticket's closeout-text state: a row of the text-state file."""

    model_config = _STRICT
    id: str = Field(min_length=1)
    last_closeout_comment_at: str = ""
    text_sha: str = ""
    held_text_sha: str = ""
    waiting_on: list[str] = Field(default_factory=list)


class ModelScopeContractStateRow(BaseModel):
    """Whether one ticket has a contract: a row of the contract-state file."""

    model_config = _STRICT
    id: str = Field(min_length=1)
    has_contract: bool


class ModelScopeWaitingRow(BaseModel):
    model_config = _STRICT
    id: str = Field(min_length=1)
    reason: str


class ModelScopeBundleTotals(BaseModel):
    model_config = _STRICT
    candidates: int = Field(ge=0)
    eligible: int = Field(ge=0)
    selected: int = Field(ge=0)
    deferred: int = Field(ge=0)
    held: int = Field(ge=0)
    waiting: int = Field(ge=0)


class ModelScopeBundle(BaseModel):
    """The bounded scope the closer selects from: what ``bundle`` prints and ``read-bundle`` reads."""

    model_config = _STRICT
    # The marker the workflow script checks: the bundle it was handed is the node's own output,
    # not text an agent typed. The script holds no field check beyond this one.
    schema_version: Literal["scope_bundle.v1"] = "scope_bundle.v1"
    candidates: list[ModelScopeCandidateRow]
    pr_state: list[ModelScopePrStateRow]
    text_state: list[ModelScopeTextStateRow]
    held: list[str]
    held_count: int = Field(ge=0)
    waiting: list[ModelScopeWaitingRow]
    waiting_count: int = Field(ge=0)
    waiting_by_reason: dict[str, int]
    no_contract: list[str]
    totals: ModelScopeBundleTotals

    @model_validator(mode="after")
    def _counts_agree(self) -> Self:
        if len(self.candidates) != self.totals.selected:
            raise ValueError(
                f"candidates carries {len(self.candidates)} rows but totals.selected is "
                f"{self.totals.selected}"
            )
        return self


def _loc(path: tuple[str | int, ...]) -> str:
    return ".".join(str(p) for p in path) or "<root>"


def _validated[M: BaseModel](
    model: type[M], document: object, file: str, prefix: str
) -> M:
    try:
        return model.model_validate(document)
    except ValidationError as exc:
        fields = tuple(prefix + _loc(e["loc"]) for e in exc.errors())
        detail = "; ".join(
            f"{prefix}{_loc(e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ScopeContractError(file, fields, detail) from exc


def _json_document(text: str, file: str) -> object:
    if not text.strip():
        raise ScopeContractError(
            file, (), "empty; a scope file the reader cannot read is never a zero"
        )
    try:
        document: object = json.loads(text)
    except ValueError as exc:
        raise ScopeContractError(file, (), f"not JSON: {exc}") from exc
    return document


def parse_scope_rows[M: BaseModel](text: str, model: type[M], *, file: str) -> list[M]:
    """Parse ``text`` as a JSON array of ``model``; raise ScopeContractError naming file and field."""
    document = _json_document(text, file)
    if not isinstance(document, list):
        raise ScopeContractError(
            file, (), f"not a JSON array ({type(document).__name__})"
        )
    return [_validated(model, row, file, f"[{i}].") for i, row in enumerate(document)]


def parse_scope_bundle(text: str, *, file: str) -> ModelScopeBundle:
    """The reader of the scope bundle: a typed bundle, or ScopeContractError naming file and field."""
    return _validated(ModelScopeBundle, _json_document(text, file), file, "")
