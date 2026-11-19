# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the JSON ``onex models list`` and ``onex models connections`` print.

The TUI runs those commands and validates their output against the model
setup node's own result models here, so no other node imports that node's
models (OMN-9263). A line that does not validate raises ``ValueError``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from omnimarket.nodes.node_model_setup_effect.models.model_endpoint_connection import (
    ModelEndpointConnectionResult,
)
from omnimarket.nodes.node_model_setup_effect.models.model_model_setup_result import (
    ModelModelStatus,
)


def model_status_rows(lines: Iterable[str]) -> list[dict[str, Any]]:
    """Each non-blank ``onex models list --json`` line, validated, as JSON data."""
    return [
        ModelModelStatus.model_validate_json(line).model_dump(mode="json")
        for line in lines
        if line.strip()
    ]


def connection_rows(text: str) -> tuple[str, list[dict[str, Any]]]:
    """``onex models connections --json``: its status and its connections."""
    result = ModelEndpointConnectionResult.model_validate_json(text)
    return result.status, [row.model_dump(mode="json") for row in result.connections]


__all__: list[str] = ["connection_rows", "model_status_rows"]
