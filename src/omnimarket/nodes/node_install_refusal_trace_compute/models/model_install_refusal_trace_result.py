# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refusals by cause: a count per class, the classes no record reached, and records no class names."""

from pydantic import BaseModel, ConfigDict, Field


class ModelRefusalClassCount(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cause: str = Field(description="The cause class, named from the record's own text.")
    install_proof: bool = Field(
        description="True when the class is a refusal for want of installed proof."
    )
    count: int = Field(ge=0)
    sources: tuple[str, ...] = Field(
        description="The sources of the records in this class, in the order read."
    )


class ModelInstallRefusalTraceResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    window_start: str
    window_end: str
    total: int = Field(ge=0, description="Records read.")
    install_proof_total: int = Field(
        ge=0,
        description="Records in a class that is a refusal for want of installed proof.",
    )
    classes: tuple[ModelRefusalClassCount, ...] = Field(
        description="Classes with at least one record, largest first."
    )
    unread_classes: tuple[str, ...] = Field(
        description="Declared classes no record in the window reached: their cause is unread, not zero."
    )
    unclassified: tuple[str, ...] = Field(
        description="Sources of records whose text names no declared class."
    )
