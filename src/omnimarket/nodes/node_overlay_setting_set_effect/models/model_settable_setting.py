# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One setting the contract declares settable through the store."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.overlay_store.model_overlay_store import OverlayId

_PLACEHOLDER = re.compile(r"^\{([a-z_]+)\}$")


def _escape(name: str) -> str:
    return name.replace("~", "~0").replace("/", "~1")


class ModelSettableSetting(BaseModel):
    """``key`` is what an operator names (dotted, a whole segment may be a ``{name}`` placeholder that stands for any one
    name, such as a lane), ``path`` is where it sits in the document (the refresh's setting path, ``/``-joined, with the
    same placeholders), and ``document_model`` is the model the whole overlay must still satisfy once the value is set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay_id: OverlayId
    key: str = Field(min_length=1)
    path: str = Field(min_length=1)
    document_model: str = Field(min_length=1)

    @model_validator(mode="after")
    def _placeholders_agree(self) -> ModelSettableSetting:
        key_names = [
            m.group(1) for s in self.key.split(".") if (m := _PLACEHOLDER.match(s))
        ]
        path_names = [
            m.group(1) for s in self.path.split("/") if (m := _PLACEHOLDER.match(s))
        ]
        if key_names != path_names:
            raise ValueError(
                f"{self.key}: the key and the path name different placeholders"
            )
        return self

    def resolve(self, key: str) -> str | None:
        """The setting path ``key`` names under this declaration, or ``None`` when the key is not of this declaration."""
        declared = self.key.split(".")
        given = key.split(".")
        if len(declared) != len(given) or any(not part for part in given):
            return None
        names: dict[str, str] = {}
        for want, got in zip(declared, given, strict=True):
            placeholder = _PLACEHOLDER.match(want)
            if placeholder is not None:
                names[placeholder.group(1)] = got
            elif want != got:
                return None
        segments: list[str] = []
        for segment in self.path.split("/"):
            placeholder = _PLACEHOLDER.match(segment)
            segments.append(
                _escape(names[placeholder.group(1)]) if placeholder else segment
            )
        return "/".join(segments)


__all__ = ["ModelSettableSetting"]
