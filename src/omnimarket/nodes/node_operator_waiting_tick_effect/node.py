"""Declarative effect node of the operator-waiting tick's list and acting row (OMN-19854)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ModelOperatorWaiting, ModelOperatorWaitingRequest

CONTRACT = Path(__file__).with_name("contract.yaml")


class NodeOperatorWaitingTickEffect:
    contract_path = CONTRACT

    def handle(self, request: ModelOperatorWaitingRequest) -> ModelOperatorWaiting:
        from .handler import HandlerOperatorWaitingTick

        return HandlerOperatorWaitingTick().handle(request)


def ask_question(cells: list[str]) -> str:
    """The question of a MSG row's cells when the row is an ask, else an empty string (see ``handler.ask_question``)."""
    from .handler import ask_question as _ask_question

    return _ask_question(cells)
