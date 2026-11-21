# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The approved-work file the fill tick reads (OMN-26223).

One JSON file persists between runs: ``{"version": 1, "rows": [...]}``. Both readers of the fill
tick (the list reader and the row reader) parse it with this model, so a file whose version is not
1 or whose rows are not an array is refused with the field named. Each row is judged on its own
by the approved-work handler, which counts invalid rows; this model is the envelope.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError


class ApprovedWorkFileError(ValueError):
    """An approved-work file that is not what its model reads: names the file and the field."""

    def __init__(self, file: str, fields: tuple[str, ...], detail: str) -> None:
        self.file = file
        self.fields = fields
        super().__init__(f"{file}: {detail}")


class ModelApprovedWorkFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    version: Literal[1]
    rows: list[JsonValue]


def parse_approved_work_file(text: str, *, file: str) -> ModelApprovedWorkFile:
    """Parse the file's text; raise ApprovedWorkFileError naming the file and the field."""
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise ApprovedWorkFileError(file, (), f"not JSON: {exc}") from exc
    try:
        return ModelApprovedWorkFile.model_validate(document)
    except ValidationError as exc:
        names = tuple(
            ".".join(str(p) for p in e["loc"]) or "<root>" for e in exc.errors()
        )
        raise ApprovedWorkFileError(
            file,
            names,
            "; ".join(
                f"{n}: {e['msg']}" for n, e in zip(names, exc.errors(), strict=True)
            ),
        ) from exc
