"""Frozen model work descriptions; evaluator instructions are trusted inputs."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


def public_path(value: str) -> str:
    path = PurePosixPath(value)
    forbidden = (
        "customer",
        "secret",
        "credential",
        ".env",
        ".ssh",
    )
    if path.is_absolute() or ".." in path.parts or "\\" in value or not path.parts:
        raise ValueError("input path must be relative without traversal")
    if re.search(r"knowledge[-_]base[-_](?:internal|private)", value.lower()) or any(
        word in value.lower() for word in forbidden
    ):
        raise ValueError("input must name public data only")
    return value


class InputPin(FrozenModel):
    path: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    _public_path = field_validator("path")(public_path)


class Budgets(FrozenModel):
    max_output_tokens: int = Field(strict=True, ge=1, le=1_000_000)
    max_wall_s: float = Field(gt=0, le=86_400)
    max_turns: int = Field(strict=True, ge=1, le=1000)
    max_usd: float = Field(ge=0, le=10_000)


class CommandSpec(FrozenModel):
    argv: tuple[str, ...] = Field(min_length=1, max_length=128)
    environment: InputPin
    expected_exit: int = Field(default=0, ge=0, le=255)


class SchemaSpec(FrozenModel):
    # Serialized schema avoids a mutable dictionary inside a frozen contract.
    schema_document: str = Field(min_length=2, max_length=1_000_000, alias="schema")
    expected_json: str | None = None

    @field_validator("schema_document", mode="before")
    @classmethod
    def encode_schema(cls, value: str | dict[str, JsonValue]) -> str:
        import json

        from jsonschema import Draft202012Validator

        schema = json.loads(value) if isinstance(value, str) else value
        Draft202012Validator.check_schema(schema)
        return json.dumps(schema, sort_keys=True, separators=(",", ":"))


class DodSpec(CommandSpec):
    ticket: str = Field(pattern=r"^OMN-[0-9]+$")
    evidence_id: str = Field(min_length=1)
    base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    repair_commit: str = Field(pattern=r"^[0-9a-f]{40}$")


class SeededBugSpec(CommandSpec):
    patch: InputPin
    target: str = Field(min_length=1)
    _public_path = field_validator("target")(public_path)


class CommandAcceptance(FrozenModel):
    kind: Literal["command"]
    spec: CommandSpec


class SchemaAcceptance(FrozenModel):
    kind: Literal["schema"]
    spec: SchemaSpec


class DodAcceptance(FrozenModel):
    kind: Literal["dod_evidence"]
    spec: DodSpec


class SeededBugAcceptance(FrozenModel):
    kind: Literal["seeded_bug"]
    spec: SeededBugSpec


Acceptance = Annotated[
    CommandAcceptance | SchemaAcceptance | DodAcceptance | SeededBugAcceptance,
    Field(discriminator="kind"),
]


class Controls(FrozenModel):
    positive: InputPin
    negative: InputPin


class Provenance(FrozenModel):
    source: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    adaptation: str = Field(min_length=1)


class ModelWorkContract(FrozenModel):
    contract_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    task_kind: str = Field(min_length=1, max_length=128)
    inputs: tuple[InputPin, ...] = Field(min_length=1)
    acceptance: Acceptance
    budgets: Budgets
    controls: Controls | None = None
    evaluators: tuple[InputPin, ...] = ()
    provenance: Provenance | None = None

    def digest(self) -> str:
        from hashlib import sha256

        return sha256(self.model_dump_json(by_alias=True).encode()).hexdigest()
