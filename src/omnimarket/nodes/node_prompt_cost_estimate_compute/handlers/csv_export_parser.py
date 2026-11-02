# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read a CSV upload by the import schema the contract declares.

No column name lives here. The required column, the optional ones and each token
column's accepted names, in precedence order, come from ``config.csv_import_schema``
in the node's contract, so a name added there is read with no code change, and
the page that tells a visitor what to upload reads the same declaration. A row
with no token count is read by text length and says which side was estimated.
"""

from __future__ import annotations

import csv
import io

from omnimarket.nodes.node_prompt_cost_estimate_compute.handlers.export_records import (
    ExportParseError,
    ParsedPrompt,
    estimate_tokens,
    parse_iso,
    token_method,
)
from omnimarket.nodes.node_prompt_cost_estimate_compute.models import (
    ModelCsvImportSchema,
)


def _first(row: dict[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        value = (row.get(name) or "").strip()
        if value:
            return value
    return ""


def _count(row: dict[str, str], names: tuple[str, ...]) -> int | None:
    for name in names:
        value = (row.get(name) or "").strip()
        if value:
            try:
                count = int(value)
            except ValueError as exc:
                raise ExportParseError(
                    f"column {name} holds {value!r}, not a whole number"
                ) from exc
            if count < 0:
                raise ExportParseError(f"column {name} holds a negative count")
            return count
    return None


def parse_csv(content: str, schema: ModelCsvImportSchema) -> list[ParsedPrompt]:
    """One record per non-empty prompt row; refuses a CSV missing a required column."""
    reader = csv.DictReader(io.StringIO(content))
    fields = {name.strip().lower() for name in reader.fieldnames or ()}
    required = schema.required_columns
    optional = schema.optional_columns
    for column, aliases in required.model_dump().items():
        if not fields.intersection(aliases):
            raise ExportParseError(
                f"the CSV has no {column} column (accepted: {', '.join(aliases)})"
            )
    prompts: list[ParsedPrompt] = []
    for raw in reader:
        row = {(key or "").strip().lower(): (value or "") for key, value in raw.items()}
        text = _first(row, required.prompt)
        if not text:
            continue
        tokens_in = _count(row, optional.tokens_in)
        tokens_out = _count(row, optional.tokens_out)
        prompts.append(
            ParsedPrompt(
                conversation_id=_first(row, optional.conversation_id),
                occurred_at=parse_iso(_first(row, optional.timestamp)),
                prompt_text=text,
                model=_first(row, optional.model),
                tokens_in=tokens_in if tokens_in is not None else estimate_tokens(text),
                tokens_out=(
                    tokens_out
                    if tokens_out is not None
                    else estimate_tokens(_first(row, optional.response))
                ),
                token_method=token_method(
                    input_estimated=tokens_in is None,
                    output_estimated=tokens_out is None,
                ),
            )
        )
    return prompts
