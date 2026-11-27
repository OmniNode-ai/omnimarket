# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The integration batch's record file (OMN-26223).

The sweep writes ``drift/integration/<date>.yaml`` and later readers (the integration-pass tick,
the prove-running reads) consume it. The producer builds the file through this model, so a
record the model refuses is never written; a reader parses with the same model and gets a
``IntegrationRecordError`` naming the file and the field, never an empty record.
"""

from __future__ import annotations

from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

_STRICT = ConfigDict(extra="forbid", frozen=True, strict=True)


class IntegrationRecordError(ValueError):
    """An integration record file that is not what its model reads."""

    def __init__(self, file: str, fields: tuple[str, ...], detail: str) -> None:
        self.file = file
        self.fields = fields
        super().__init__(f"{file}: {detail}")


class ModelRuntimeShaRecord(BaseModel):
    model_config = _STRICT
    ticket_id: str = Field(min_length=1)
    evidence_item_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    merge_sha: str = Field(min_length=1)
    receipt_path: str | None = None


class ModelIntegrationRecord(BaseModel):
    model_config = _STRICT
    artifact_type: Literal["ModelIntegrationRecord"] = "ModelIntegrationRecord"
    schema_version: Literal["1.0.0"] = Field(default="1.0.0", alias="artifact_version")
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    scope: str = Field(min_length=1)
    status: Literal["recorded", "blocked", "planned", "no_input"]
    tickets: list[str]
    runtime_sha_match: list[ModelRuntimeShaRecord]
    surfaces: list[dict[str, JsonValue]]

    def to_document(self) -> dict[str, JsonValue]:
        return self.model_dump(mode="json", by_alias=True)


def read_integration_record(text: str, *, file: str) -> ModelIntegrationRecord:
    """Parse a record file; raise IntegrationRecordError naming the file and the field."""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise IntegrationRecordError(file, (), f"not YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise IntegrationRecordError(
            file, (), f"not a mapping ({type(document).__name__})"
        )
    try:
        return ModelIntegrationRecord.model_validate(document)
    except ValidationError as exc:
        fields = tuple(".".join(str(p) for p in e["loc"]) for e in exc.errors())
        detail = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise IntegrationRecordError(file, fields, detail) from exc
