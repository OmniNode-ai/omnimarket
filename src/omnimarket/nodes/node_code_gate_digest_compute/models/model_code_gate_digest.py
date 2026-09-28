# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input and output of the code gate digest compute (OMN-19527).

The input is what the repository's own lint and type gates printed about ONE
delegated file, plus that file's source: ``ruff check``, ``ruff format
--check`` and ``mypy --strict``, each with its exit code, run inside the target
repository's checkout so the repository's own configuration applies. The
output is a bounded digest small enough to paste verbatim into a repair prompt,
and a fingerprint so a loop can tell the same findings from new ones.

A gate that did not run, or exited with a code that means the tool itself
failed, makes the digest ``infra_error``: an unrun gate is never read as clean.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

#: Cap on one tool's captured output.
MAX_GATE_OUTPUT_CHARS = 65_536
#: Cap on the findings carried in the digest; the count is kept whole.
MAX_FINDINGS = 25
#: Cap on one finding's message.
MAX_FINDING_MESSAGE_CHARS = 200
#: Cap on the rendered digest text a repair prompt carries verbatim.
MAX_DIGEST_CHARS = 2000


class EnumCodeGate(StrEnum):
    """The gates a delegated file is run through."""

    RUFF_CHECK = "ruff_check"
    RUFF_FORMAT = "ruff_format"
    MYPY = "mypy"
    #: Computed here from the source: ONEX refuses typing.Any
    #: (onex-validate-any-types), and neither ruff nor mypy --strict does.
    ANY_TYPES = "any_types"


#: The gates whose output the caller must supply, in run order.
TOOL_GATES: tuple[EnumCodeGate, ...] = (
    EnumCodeGate.RUFF_CHECK,
    EnumCodeGate.RUFF_FORMAT,
    EnumCodeGate.MYPY,
)


class ModelGateToolOutput(BaseModel):
    """One tool's run over the delegated file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: EnumCodeGate
    exit_code: int | None = Field(
        ..., description="The tool's exit code; None when it never ran."
    )
    output: str = Field(default="", max_length=MAX_GATE_OUTPUT_CHARS)


class ModelCodeGateDigestRequest(BaseModel):
    """The delegated file, and what each tool gate printed about it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(
        ...,
        min_length=1,
        max_length=512,
        description="The file's path relative to the repository root.",
    )
    source: str = Field(
        default="", description="The file's content, for the typing.Any gate."
    )
    outputs: tuple[ModelGateToolOutput, ...] = ()


class ModelGateFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: EnumCodeGate
    line: int = Field(..., ge=0, description="1-based; 0 when the whole file.")
    code: str = Field(..., min_length=1, max_length=64)
    message: str = Field(default="", max_length=MAX_FINDING_MESSAGE_CHARS)


class ModelCodeGateDigest(BaseModel):
    """What the gates said, bounded. ``clean`` only when every gate ran clean."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    clean: bool
    infra_error: bool = Field(
        default=False,
        description="A tool gate did not run, or failed as a tool. Never clean.",
    )
    findings: tuple[ModelGateFinding, ...] = Field(default=(), max_length=MAX_FINDINGS)
    finding_count: int = Field(default=0, ge=0)
    truncated: bool = False
    gates_run: tuple[EnumCodeGate, ...] = ()
    digest_text: str = Field(default="", max_length=MAX_DIGEST_CHARS)
    fingerprint: str = Field(
        default="",
        description="sha256 over the sorted (gate, code, message) findings and "
        "the infra faults; line numbers are left out, so a finding that only "
        "moved keeps its fingerprint. Empty when clean.",
    )


__all__ = [
    "MAX_DIGEST_CHARS",
    "MAX_FINDINGS",
    "MAX_FINDING_MESSAGE_CHARS",
    "MAX_GATE_OUTPUT_CHARS",
    "TOOL_GATES",
    "EnumCodeGate",
    "ModelCodeGateDigest",
    "ModelCodeGateDigestRequest",
    "ModelGateFinding",
    "ModelGateToolOutput",
]
