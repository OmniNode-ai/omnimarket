# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire types shared by error-rule matching, exception emission and replay.

Rule validation belongs to core. A table here is a caller-supplied, already
validated snapshot; integer row versions explicitly bind normalizer fact hashes.
"""

from pathlib import Path
from typing import Annotated, Literal

from omnibase_core.models.error_rule_table.model_error_rule_action import (
    ModelErrorRuleAction,
)
from omnibase_core.models.error_rule_table.model_error_rule_row import ModelErrorRuleRow
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.error_normalizer import (
    ModelErrorClassAlias,
    ModelErrorClassNormalizeResult,
    ModelErrorEvent,
    ModelErrorNormalizerFacts,
)
from omnimarket.models.exception_queue import ModelExceptionEmitResult


class ModelErrorRuleContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    host_role: str = ""
    repository: str = ""
    ticket_state: str = ""
    state_checks: tuple[str, ...] = ()
    trigger_kind: Literal["text", "state", "repeat"] = "text"


class ModelErrorRuleKeyVersion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    row_version: int = Field(gt=0, strict=True)
    normalizer_version: str = Field(pattern=r"^[0-9a-f]{64}$")


class ModelErrorRuleTableDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    error_rules: tuple[ModelErrorRuleRow, ...]


class ModelErrorRuleTable(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    rows: tuple[ModelErrorRuleRow, ...]
    key_versions: tuple[ModelErrorRuleKeyVersion, ...]


class ModelErrorRuleMatchRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    normalized: ModelErrorClassNormalizeResult
    table: ModelErrorRuleTable
    context: ModelErrorRuleContext = Field(default_factory=ModelErrorRuleContext)


class ModelErrorRuleNoMatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["NO-MATCH"] = "NO-MATCH"
    class_key: str
    key_version: str


class ModelErrorRuleMatched(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["MATCH"] = "MATCH"
    class_key: str
    key_version: str
    row_id: str
    rules: tuple[str, ...]
    action: ModelErrorRuleAction
    mode: str


class ModelErrorRuleMatchResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    decision: Annotated[
        ModelErrorRuleMatched | ModelErrorRuleNoMatch, Field(discriminator="status")
    ]


class ModelErrorRuleNoMatchOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    state_dir: Path
    emit_node: str
    class_prefix: str
    class_hash_length: int = Field(gt=0, le=64)
    source: str
    tried: str
    proposed_fix: str
    runner: Path | None = None
    parent: str | None = None
    role: str = ""


class ModelErrorRuleNoMatchRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    decision: ModelErrorRuleNoMatch
    overlay: ModelErrorRuleNoMatchOverlay
    evidence: str
    now: str
    repository: str = ""
    ticket: str = ""


class ModelErrorRuleNoMatchResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    class_key: str
    queued: ModelExceptionEmitResult


class ModelErrorRuleReplayRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    event: ModelErrorEvent
    context: ModelErrorRuleContext = Field(default_factory=ModelErrorRuleContext)
    gold: Literal["unlabelled", "MATCH", "NO-MATCH"] = "unlabelled"
    expected_row_id: str | None = None


class ModelErrorRuleReplayRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    records: tuple[ModelErrorRuleReplayRecord, ...]
    facts: ModelErrorNormalizerFacts
    table: ModelErrorRuleTable
    aliases: tuple[ModelErrorClassAlias, ...] = ()
    key_version: str | None = None


class ModelErrorRuleReplayResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    hits: int
    misses: int
    false_matches: int
    labelled: int
    unlabelled: int
    no_match_keys: tuple[str, ...]

    def report(self) -> str:
        return (
            "\n".join(
                (
                    f"hits={self.hits} misses={self.misses} false_matches={self.false_matches} "
                    f"labelled={self.labelled} unlabelled={self.unlabelled}",
                    *(f"NO-MATCH {key}" for key in self.no_match_keys),
                )
            )
            + "\n"
        )
