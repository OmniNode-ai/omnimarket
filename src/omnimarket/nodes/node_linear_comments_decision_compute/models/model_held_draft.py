# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A held draft of the hourly Linear-comments sweep (OMN-26223).

A human ask that reached drafting and came back without an accepted draft is a held entry in
the sweep's file. The producer builds each entry through this model; the renderer and the
tick read the list with it, so a held list that is not a list of these is refused with the
field named, never counted as zero.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# The sweep script's closed set of reasons (its HELD_CLASSES); the decision node's
# EnumHeldClass does not yet carry no_consumer, so the entry names the script's set.
HeldReasonClass = Literal[
    "floor_refused", "truncated", "unsourced", "no_consumer", "transport", "timeout"
]


class HeldDraftListError(ValueError):
    """A held list that is not a list of held drafts: names the file and each failing field."""

    def __init__(self, file: str, fields: tuple[str, ...], detail: str) -> None:
        self.file = file
        self.fields = fields
        super().__init__(f"{file}: {detail}")


class ModelHeldDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    ticket: str = Field(min_length=1)
    comment_id: str = Field(min_length=1)
    comment_url: str = ""
    author_role: str
    stage: str = Field(min_length=1)
    reason_class: HeldReasonClass
    reason: str
    asks: list[str]


def read_held_drafts(document: object, *, file: str) -> list[ModelHeldDraft]:
    """Parse a decoded JSON held list; raise HeldDraftListError naming the file and the field."""
    if not isinstance(document, list):
        raise HeldDraftListError(
            file, ("held",), f"held is not an array ({type(document).__name__})"
        )
    held: list[ModelHeldDraft] = []
    for index, entry in enumerate(document):
        try:
            held.append(ModelHeldDraft.model_validate(entry))
        except ValidationError as exc:
            fields = tuple(
                f"held[{index}]." + ".".join(str(p) for p in e["loc"])
                for e in exc.errors()
            )
            raise HeldDraftListError(
                file,
                fields,
                "; ".join(
                    f"{f}: {e['msg']}"
                    for f, e in zip(fields, exc.errors(), strict=True)
                ),
            ) from exc
    return held
