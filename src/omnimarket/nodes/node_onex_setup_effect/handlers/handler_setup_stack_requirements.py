# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Observe the selected stack's real engine, declared resources and health.

No Docker Desktop settings are changed or license accepted. Resource minima come
only from selected services' Compose reservations, never universal host floors.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any

from omnibase_infra.nodes.node_setup_local_provision_effect.handlers.handler_local_provision import (
    compose_base_command,
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

    def __init__(self) -> None:
        self._commands = HandlerSetupCommand()

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
        selection = plan.local_stack
        if selection is None or not required:
            return ModelSetupObservation(
                step=self.name,
                status="action_required",
                fix="Select explicit compose/topology services before checking Docker.",
            )
        try:
            info = json.loads(self._read(("docker", "info", "--format", "{{json .}}")))
            base = tuple(compose_base_command(selection.compose_file, None))
            config = json.loads(self._read((*base, "config", "--format", "json")))
            if (
                not isinstance(info, dict)
                or not isinstance(config, dict)
                or not isinstance(config.get("services"), dict)
            ):
                raise ValueError("Malformed Docker observation")
            available_cpu = float(info["NCPU"])
            available_memory = int(info["MemTotal"])
            if (
                not math.isfinite(available_cpu)
                or available_cpu <= 0
                or available_memory <= 0
            ):
                raise ValueError("Invalid engine resources")
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

    def handle(
        self, request: ModelSetupStackRequirementsRequest
    ) -> ModelSetupObservation:
        return self.observe(request.plan, request.services)
