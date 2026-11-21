# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Observe the selected stack's real engine, declared resources and health.

No Docker Desktop settings are changed or license accepted. Resource minima come
only from selected services' Compose reservations, never universal host floors.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Callable, Coroutine, Mapping
from typing import Any

from omnibase_core.utils.util_run_coro_sync import run_coro_sync
from omnibase_infra.nodes.node_setup_local_provision_effect.handlers.handler_local_provision import (
    compose_base_command,
)

from omnimarket.nodes.node_onex_setup_effect.adapters.adapter_local_engine_info import (
    read_engine_capacity,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_command import (
    HandlerSetupCommand,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_command_request import (
    ModelSetupCommandRequest,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import (
    ModelSetupObservation,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_stack_requirements import (
    ModelSetupStackRequirementsRequest,
)

EngineCapacityReader = Callable[[], Coroutine[Any, Any, tuple[float, int]]]
# Every reader, injected or infra's, gets the limit the default passes to infra.
_ENGINE_READ_TIMEOUT_SECONDS = 20


def _bytes(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if not isinstance(value, str):
        raise ValueError("Invalid memory reservation")
    match = re.fullmatch(
        r"([0-9]+(?:\.[0-9]+)?)\s*([kmgtp]?)(?:i?b)?", value.strip(), re.IGNORECASE
    )
    if match is None:
        raise ValueError("Invalid memory reservation")
    power = "kmgtp".index(match[2].lower()) + 1 if match[2] else 0
    return math.ceil(float(match[1]) * (1 << (10 * power)))


class HandlerSetupStackRequirements:
    """Read Docker without changing the selected stack or operator settings."""

    name = "local-stack"

    def __init__(
        self, engine_reader: EngineCapacityReader = read_engine_capacity
    ) -> None:
        """The engine reader is injected so the adapter, not this handler, builds the infra handler."""
        self._commands = HandlerSetupCommand()
        self._engine_reader = engine_reader

    def _read(self, argv: tuple[str, ...]) -> str:
        result = self._commands.handle(
            ModelSetupCommandRequest(argv=argv, timeout_seconds=20)
        )
        if result.returncode != 0:
            raise OSError("Docker observation unavailable")
        if len(result.stdout.encode()) > 4 * 1024 * 1024:
            raise ValueError("Docker observation is oversized")
        return result.stdout

    @staticmethod
    def _rows(raw: str) -> list[dict[str, Any]]:
        if not raw.strip():
            return []
        if raw.lstrip().startswith("["):
            value = json.loads(raw)
            if not isinstance(value, list) or any(
                not isinstance(row, dict) for row in value
            ):
                raise ValueError("Malformed Compose status")
            return value
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError("Malformed Compose status")
        return rows

    def observe(
        self, plan: ModelSetupPlan, required: tuple[str, ...]
    ) -> ModelSetupObservation:
        """Synchronous entry for callers that own no event loop."""
        return run_coro_sync(self.observe_async(plan, required))

    async def observe_async(
        self, plan: ModelSetupPlan, required: tuple[str, ...]
    ) -> ModelSetupObservation:
        """Read the engine on the caller's loop, so a reader may share that loop."""
        selection = plan.local_stack
        if selection is None or not required:
            return ModelSetupObservation(
                step=self.name,
                status="action_required",
                fix="Select explicit compose/topology services before checking Docker.",
            )
        try:
            available_cpu, available_memory = await asyncio.wait_for(
                self._engine_reader(), timeout=_ENGINE_READ_TIMEOUT_SECONDS
            )
            # An injected reader is not bound by infra's checks; NaN compares as enough.
            if not (
                math.isfinite(available_cpu)
                and available_cpu > 0
                and available_memory > 0
            ):
                raise ValueError("Invalid engine resources")
            base = tuple(compose_base_command(selection.compose_file, None))
            config = json.loads(self._read((*base, "config", "--format", "json")))
            if not isinstance(config, dict) or not isinstance(
                config.get("services"), dict
            ):
                raise ValueError("Malformed Docker observation")
            cpus, memory = 0.0, 0
            service_config = config["services"]
            for name in required:
                service = service_config.get(name)
                if not isinstance(service, dict):
                    raise ValueError("Selected service missing from Compose")
                reserve = (
                    service.get("deploy", {})
                    .get("resources", {})
                    .get("reservations", {})
                )
                if not isinstance(reserve, Mapping):
                    raise ValueError("Invalid Compose reservations")
                cpu = float(reserve.get("cpus", 0))
                if not math.isfinite(cpu) or cpu < 0:
                    raise ValueError("Invalid CPU reservation")
                cpus += cpu
                memory += _bytes(reserve.get("memory", 0))
            evidence = {
                "resource_basis": "selected service reservations only",
                "required_cpus": str(cpus),
                "required_memory_bytes": str(memory),
                "engine_cpus": str(available_cpu),
                "engine_memory_bytes": str(available_memory),
            }
            if cpus > available_cpu or memory > available_memory:
                return ModelSetupObservation(
                    step=self.name,
                    status="action_required",
                    evidence=evidence,
                    fix="The selected services reserve more resources than the Docker engine exposes. Adjust the selected Compose reservations or Docker resources, then retry. No settings were changed.",
                )
            rows = self._rows(self._read((*base, "ps", "--all", "--format", "json")))
            for name in required:
                selected = [row for row in rows if row.get("Service") == name]
                healthcheck = service_config[name].get("healthcheck", {})
                needs_health = bool(healthcheck) and not healthcheck.get(
                    "disable", False
                )
                if not selected or any(
                    row.get("State") != "running"
                    or row.get("Health") in ("unhealthy", "starting")
                    or (needs_health and row.get("Health") != "healthy")
                    for row in selected
                ):
                    return ModelSetupObservation(
                        step=self.name,
                        status="unavailable",
                        evidence=evidence,
                        fix=f"Selected service {name} is missing, stopped or not healthy. Repair this stack before model setup proof.",
                    )
            return ModelSetupObservation(
                step=self.name, status="success", evidence=evidence
            )
        except ModuleNotFoundError as exc:
            if exc.name != "omnibase_infra.handlers.handler_local_engine_info":
                raise
            return ModelSetupObservation(
                step=self.name,
                status="action_required",
                fix="Upgrade omnimarket and omnibase-infra to compatible releases that support local-stack engine checks, then retry.",
            )
        except OSError:
            return ModelSetupObservation(
                step=self.name,
                status="action_required",
                fix="Docker or Compose could not be queried. Open Docker Desktop if used, review any pending terms yourself, and check daemon access and Compose support before retrying. No license or settings were changed.",
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            return ModelSetupObservation(
                step=self.name,
                status="failure",
                fix="Docker returned an invalid or incomplete engine, configuration or health observation. Repair the selected stack inputs and retry; raw process output was withheld.",
            )

    async def handle(
        self, request: ModelSetupStackRequirementsRequest
    ) -> ModelSetupObservation:
        """Bus entry: the runtime awaits this on its own loop."""
        return await self.observe_async(request.plan, request.services)
