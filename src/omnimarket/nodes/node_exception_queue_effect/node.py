"""Declarative effect node for the exception queue (OMN-17427)."""

from __future__ import annotations

from pathlib import Path

from .handler import HandlerExceptionQueueDispatch

CONTRACT = Path(__file__).with_name("contract.yaml")


class NodeExceptionQueueEffect(HandlerExceptionQueueDispatch):
    """Expose the exception queue handler through its node contract."""

    contract_path = CONTRACT
