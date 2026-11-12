# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders shared by the host install state reducer tests (OMN-23957)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import yaml

from omnimarket.nodes.node_host_install_state_reducer.models import (
    EnumHostInstallOutcome,
    EnumHostInstallStep,
    ModelHostInstallRecord,
    ModelHostInstallStepEvent,
)
from omnimarket.nodes.node_host_install_state_reducer.overlay import (
    ModelHostInstallStateOverlay,
)

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_host_install_state_reducer"
)
START = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
HOST = "host-a"
UNIT = "unit-a"
RUN_ID = uuid4()


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


RENDER = digest("rendered unit")
PREVIOUS = digest("previous unit")
OTHER = digest("hand-edited unit")


def overlay() -> ModelHostInstallStateOverlay:
    document = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    return ModelHostInstallStateOverlay.model_validate(document)


def event(
    step: EnumHostInstallStep,
    outcome: EnumHostInstallOutcome = EnumHostInstallOutcome.OK,
    *,
    minute: int = 0,
    correlation_id: UUID = RUN_ID,
    host: str = HOST,
    unit: str = UNIT,
    actor: str = "actor-a",
    **cells: str,
) -> ModelHostInstallStepEvent:
    return ModelHostInstallStepEvent(
        correlation_id=correlation_id,
        host=host,
        unit=unit,
        step=step,
        outcome=outcome,
        actor=actor,
        at=START + timedelta(minutes=minute),
        **cells,
    )


S = EnumHostInstallStep
O = EnumHostInstallOutcome  # noqa: E741


def request(**cells: str) -> ModelHostInstallStepEvent:
    return event(S.REQUEST, render_digest=cells.pop("render_digest", RENDER), **cells)


def golden_chain() -> list[ModelHostInstallStepEvent]:
    """Request, snapshot, install (started then ok), verify with the render digest, one ok run."""
    return [
        request(),
        event(S.SNAPSHOT, minute=1, rollback_target_digest=PREVIOUS),
        event(S.INSTALL, O.STARTED, minute=2),
        event(S.INSTALL, minute=3, installed_digest=RENDER),
        event(S.VERIFY, minute=4, installed_digest=RENDER),
        event(S.RUN, minute=5),
    ]


def error_chain() -> list[ModelHostInstallStepEvent]:
    """The verify read finds another file than the render: the record fails and the rollback restores the previous file."""
    return [
        request(),
        event(S.SNAPSHOT, minute=1, rollback_target_digest=PREVIOUS),
        event(S.INSTALL, minute=2, installed_digest=RENDER),
        event(S.VERIFY, minute=3, installed_digest=OTHER),
        event(S.ROLLBACK, minute=4, installed_digest=PREVIOUS),
    ]


def fold_all(
    events: list[ModelHostInstallStepEvent],
    prior: ModelHostInstallRecord | None = None,
) -> ModelHostInstallRecord:
    from omnimarket.nodes.node_host_install_state_reducer.handlers.handler_host_install_state import (
        fold,
    )

    record = prior
    for item in events:
        record = fold(record, item, overlay())
    assert record is not None
    return record
