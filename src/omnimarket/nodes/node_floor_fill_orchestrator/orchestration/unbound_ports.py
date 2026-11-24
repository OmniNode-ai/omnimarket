# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stand-ins for a pool or tickets seam that has no reader: they fail closed and name what is unbound.

Every read raises ``FloorFillUnreadableError`` with ``unbound:pool`` or ``unbound:tickets``, and the mint answers
``unavailable`` naming the same cause. The live profile binds its own readers and binds none of these
(``FloorFillOrchestrator.unbound_seams`` names any that is bound); they stay as the failing control of that check.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar

from omnimarket.models.floor_fill import (
    ModelFloorEntryTicketVerdict,
    ModelFloorTaskPayloadVerdict,
)
from omnimarket.models.floor_fill.model_start_criterion import (
    ModelFloorStartCriterion,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    FloorFillUnreadableError,
    ModelMintResult,
)


class UnboundFloorPool:
    """A pool seam with no reader: every read is an unreadable pool. The live profile binds none of these."""

    unbound: ClassVar[bool] = True

    def free_slots(self) -> int:
        raise FloorFillUnreadableError("unbound:pool")


class UnboundFloorTickets:
    """The tickets seam while the floor-task ticket nodes are not on the dev branch.

    Verdict reads are an unreadable tickets source; the mint fails closed as ``unavailable``.
    The live profile binds none of these.
    """

    unbound: ClassVar[bool] = True

    def payload_verdicts(
        self, floor_record: str, plan_texts: Mapping[str, str | None]
    ) -> Sequence[ModelFloorTaskPayloadVerdict]:
        raise FloorFillUnreadableError("unbound:tickets")

    def start_criteria(self, floor_record: str) -> Sequence[ModelFloorStartCriterion]:
        """No criterion: nothing is ready, so nothing can start.

        The verdict reads still refuse by name (``payload_verdicts`` and ``entry_ticket_verdicts`` raise
        ``FloorFillUnreadableError("unbound:tickets")``), and the mint answers ``unavailable``.
        """
        return ()

    def entry_ticket_verdicts(
        self, tickets: Mapping[str, str]
    ) -> Sequence[ModelFloorEntryTicketVerdict]:
        raise FloorFillUnreadableError("unbound:tickets")

    def mint(self, key: str) -> ModelMintResult:
        return ModelMintResult(state="unavailable", cause="unbound:tickets")


__all__: list[str] = ["UnboundFloorPool", "UnboundFloorTickets"]
