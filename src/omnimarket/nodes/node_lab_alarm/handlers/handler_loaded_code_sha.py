# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The loaded-code-sha read: the sha the lane's deploy agent has loaded.

The read behind the lab-pass-receipt's first step, on a typed operation of its own: the lane's
delivered sha is asked of the deploy agent's surfaces and answered with the sha and the surface
that delivered it. A surface that answers nothing usable is UNREAD and names the reason, never an
alarm and never delivered; the read carries no verdict of its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_loaded_code_sha import (
    EnumLoadedCodeShaOutcome,
    ModelLoadedCodeShaRequest,
    ModelLoadedCodeShaResult,
)
from omnimarket.nodes.node_lab_alarm.protocols import (
    ProtocolDeliveredShaSource,
    ShaUnresolvedError,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


class HandlerLoadedCodeSha:
    """Read the sha the lane's deploy agent has loaded and the surface that answered."""

    def __init__(self, sha_source: ProtocolDeliveredShaSource) -> None:
        self._sha_source = sha_source

    def handle(self, request: ModelLoadedCodeShaRequest) -> ModelLoadedCodeShaResult:
        try:
            sha, surface = self._sha_source.read()
        except ShaUnresolvedError as exc:
            return ModelLoadedCodeShaResult(
                correlation_id=request.correlation_id,
                outcome=EnumLoadedCodeShaOutcome.UNREAD,
                sha=None,
                surface=None,
                error=str(exc),
            )
        return ModelLoadedCodeShaResult(
            correlation_id=request.correlation_id,
            outcome=EnumLoadedCodeShaOutcome.READ,
            sha=sha,
            surface=surface,
        )


def events_for(
    result: ModelLoadedCodeShaResult, topics: Mapping[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The chain of one read: started, then the completed or the failed terminal."""
    cid = result.correlation_id
    chain: list[tuple[str, dict[str, object]]] = [
        (topics["loaded_code_sha_run_started"], {"correlation_id": cid})
    ]
    final = topics[
        "loaded_code_sha_run_completed"
        if result.outcome is EnumLoadedCodeShaOutcome.READ
        else "loaded_code_sha_run_failed"
    ]
    chain.append(
        (
            final,
            {
                "correlation_id": cid,
                "outcome": result.outcome.value,
                "sha": result.sha,
                "surface": result.surface,
                "error": result.error,
            },
        )
    )
    return chain


__all__ = [
    "HandlerLoadedCodeSha",
    "events_for",
    "load_topics",
]
