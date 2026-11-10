# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve the CI Summary registries the way the workflow does, for the gate tests (OMN-23505).

The gate holds no loader of the registry files: ``node_ci_summary_registry_compute``
resolves them and the gate reads the answer. These helpers run the node's handler over
the shipped files, or over a replaced file, and write the answer where the gate reads it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_ci_summary_registry_compute.handlers.handler_ci_summary_registry import (
    HandlerCiSummaryRegistry,
)
from omnimarket.nodes.node_ci_summary_registry_compute.models.model_ci_summary_registry import (
    ModelCiSummaryRegistryRequest,
)

CI_DIR = Path(__file__).resolve().parents[4] / "scripts" / "ci"


def read_shipped(name: str) -> Any:
    return json.loads((CI_DIR / name).read_text(encoding="utf-8"))


def shipped_request(**overrides: Any) -> ModelCiSummaryRegistryRequest:
    fields: dict[str, Any] = {
        "sweep_exclusions": read_shipped("ci_summary_sweep_exclusions.json"),
        "check_modes_contract": read_shipped(
            "ci_summary_external_check_modes.contract.json"
        ),
        "check_modes_overlay": read_shipped("ci_summary_external_check_modes.json"),
    }
    return ModelCiSummaryRegistryRequest(**{**fields, **overrides})


def write_resolved(path: Path, request: ModelCiSummaryRegistryRequest) -> Path:
    answer = HandlerCiSummaryRegistry().handle(request)
    path.write_text(answer.model_dump_json(), encoding="utf-8")
    return path
