# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed fields for building argv without invoking the ledger writer."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class ModelLedgerRowsBuildRequest(BaseModel):
    """Build directly, or validate ``request_data`` using the JSON request-file rules.

    Direct fields preserve ``build_row_argv`` semantics, including arbitrary kinds and
    tickets taking precedence over ticket. Non-null ``request_data`` selects
    ``build_request_argv``; ``request_mode="json"`` also validates null. An explicit
    mode keeps this choice stable when runtime serialization includes defaults.
    Malformed JSON fields produce the source refusal. No process is started and
    no ledger grammar guards are applied here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    operation: Literal["build"]
    request_mode: Literal["fields", "json"] = "fields"
    kind: str = ""
    lane: str = ""
    ticket: str | None = None
    tickets: list[str] | None = None
    actor: str | None = None
    model: str | None = None
    text: str | None = None
    extra: dict[str, str | list[str] | None] | None = None
    request_data: JsonValue = None


class ModelLedgerRowsBuildResult(BaseModel):
    """The argv to inspect or pass to a writer, or the builder's refusal message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["build"]
    argv: list[str] = Field(default_factory=list)
    refusal: str | None = None
    admitted: bool
