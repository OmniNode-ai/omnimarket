# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request prepared once: ledger rows parsed and sorted, sources indexed by name."""

from __future__ import annotations

from dataclasses import dataclass

from omnimarket.models.process_health import (
    EnumSourceName,
    ModelProcessHealthConfig,
    ModelProcessHealthRequest,
    ModelSourceRead,
)
from omnimarket.nodes.node_process_health_compute.handlers.ledger_view import (
    ParsedRow,
    parse_row,
)


@dataclass(frozen=True)
class Context:
    """Everything a calculator reads."""

    request: ModelProcessHealthRequest
    config: ModelProcessHealthConfig
    reads: dict[EnumSourceName, ModelSourceRead]
    ledger: tuple[ParsedRow, ...]


def build_context(request: ModelProcessHealthRequest) -> Context:
    """Parse the ledger rows once, in stamp order."""
    parsed = sorted(
        (parse_row(row) for row in request.ledger_rows),
        key=lambda row: (row.ts, row.rtype, row.lane),
    )
    return Context(
        request=request,
        config=request.config,
        reads={source.name: source for source in request.sources},
        ledger=tuple(parsed),
    )
