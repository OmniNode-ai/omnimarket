# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input and output of the delegated test prompt compute (OMN-19361)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: The target excerpt cap. With the prompt's fixed text and a repair digest,
#: this stays inside one 32,768-token slot on the local model host.
MAX_EXCERPT_CHARS = 12_000
MAX_PREVIOUS_TEST_CHARS = 12_000


class ModelFailureContext(BaseModel):
    """What the last run said, as the failure digest reported it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: str = Field(..., max_length=64)
    exception_type: str = Field(default="", max_length=200)
    message: str = Field(default="", max_length=500)
    frames: str = Field(default="", max_length=1500)
    failing_node_id: str = Field(default="", max_length=500)


class ModelDelegatedTestPromptRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["write", "repair"]
    criterion: str = Field(..., min_length=1, max_length=4000)
    target_path: str = Field(..., min_length=1, max_length=512)
    target_excerpt: str = Field(..., min_length=1)
    test_path: str = Field(..., pattern=r"^tests/[A-Za-z0-9_./-]+\.py$", max_length=512)
    previous_test: str = Field(default="")
    failure: ModelFailureContext | None = None
    gate_findings: str = Field(
        default="",
        max_length=2500,
        description="OMN-19527: the code gate digest text, verbatim, when the "
        "previous test PASSED but the repository's lint and type gates refused "
        "it. A repair carries either this or a failure, never neither.",
    )
    forbidden_fragments: tuple[str, ...] = Field(
        default=(),
        description="Strings that must not appear anywhere in the prompt: the "
        "hidden human test's class and function names.",
    )

    @model_validator(mode="after")
    def _repair_has_its_inputs(self) -> ModelDelegatedTestPromptRequest:
        if self.mode == "repair" and (
            not self.previous_test.strip()
            or (self.failure is None and not self.gate_findings.strip())
        ):
            raise ValueError(
                "a repair prompt needs the previous test and its failure digest "
                "or its gate findings"
            )
        return self


class ModelDelegatedTestPromptBundle(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str
    test_path: str
    response_contract: dict[str, object]
    excerpt_truncated: bool = False


__all__ = [
    "MAX_EXCERPT_CHARS",
    "MAX_PREVIOUS_TEST_CHARS",
    "ModelDelegatedTestPromptBundle",
    "ModelDelegatedTestPromptRequest",
    "ModelFailureContext",
]
