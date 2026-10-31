"""Declarative effect node of the throughput tick's host pool alarm (OMN-17427)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ModelHostPoolAlarm, ModelHostPoolAlarmRequest

CONTRACT = Path(__file__).with_name("contract.yaml")


class NodeHostPoolAlarmEffect:
    contract_path = CONTRACT

    def handle(self, request: ModelHostPoolAlarmRequest) -> ModelHostPoolAlarm:
        from .container.container_host_pool_alarm_effect import (
            create_handler_host_pool_alarm,
        )

        return create_handler_host_pool_alarm().handle(request)
