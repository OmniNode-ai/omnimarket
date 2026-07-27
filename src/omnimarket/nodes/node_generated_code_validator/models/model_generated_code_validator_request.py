"""Request model for the generated-code validator compute node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelExpectedStructure(BaseModel):
    """The node structure a generated artifact is expected to declare.

    Every field is optional: a check runs only when its expectation is set, so a
    caller can assert as much or as little of the structure as it knows. This is
    the pure, in-request stand-in for what a ``contract.yaml`` would declare
    about the concrete code — the validator performs no disk access.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    class_name: str | None = Field(
        default=None,
        description="Name of the class the artifact must define.",
    )
    base_class: str | None = Field(
        default=None,
        description="Base class the target class must inherit (the archetype, e.g. NodeCompute).",
    )
    required_methods: tuple[str, ...] = Field(
        default=(),
        description="Method names the target class must define (e.g. the handler signature `handle`).",
    )


class ModelGeneratedCodeValidatorRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_text: str = Field(
        min_length=1,
        description="Full Python source text of the generated code artifact.",
    )
    expected: ModelExpectedStructure | None = Field(
        default=None,
        description="Optional expected structure; structure checks run only when supplied.",
    )
    correlation_id: str = Field(
        default="",
        description=(
            "Opaque run identity echoed verbatim onto the verdict so a downstream "
            "reducer can rejoin the pure result to per-run state (OMN-14608). "
            "Does not affect validation; empty for direct callers."
        ),
    )


__all__ = ["ModelExpectedStructure", "ModelGeneratedCodeValidatorRequest"]
