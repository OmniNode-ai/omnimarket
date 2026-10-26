"""Declarative compute node of the controller's not-green read for the green-unlanded alarm (OMN-19998)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ModelGreenControllerReadRequest, ModelGreenControllerReadResult

CONTRACT = Path(__file__).with_name("contract.yaml")


class NodeGreenControllerReadCompute:
    contract_path = CONTRACT

    def handle(
        self, request: ModelGreenControllerReadRequest
    ) -> ModelGreenControllerReadResult:
        from .handler import HandlerGreenControllerRead

        return HandlerGreenControllerRead().handle(request)


def controller_not_green(
    landing: Path,
    heads: Mapping[str, str],
    *,
    now: datetime | None = None,
    bound_min: float | None = None,
) -> set[str]:
    """``repo#n`` of every head the controller's ``last-facts.json`` under ``landing`` reads not green-landable; a file
    that cannot be read reads nothing."""
    from .models import ModelGreenControllerReadRequest

    try:
        facts = json.loads((landing / "last-facts.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    request = ModelGreenControllerReadRequest(
        facts if isinstance(facts, dict) else None, heads, now, bound_min
    )
    return set(NodeGreenControllerReadCompute().handle(request).not_green)
