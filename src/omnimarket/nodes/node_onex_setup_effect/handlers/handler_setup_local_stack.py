# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Optional supplied stack reconciliation through the existing infra owners.

The authoritative setup journal retains the applied input digest. No separate
provisioning engine, stack definition, data teardown or resource claim is made.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import stat
from contextvars import ContextVar
from pathlib import Path
from uuid import uuid4

import yaml
from omnibase_core.cli.cli_user_config import user_config_path
from omnibase_core.models.container.model_onex_container import ModelONEXContainer
from omnibase_core.models.core.model_deployment_topology import ModelDeploymentTopology
from omnibase_infra.nodes.node_setup_local_provision_effect.handlers.handler_local_provision import (
    HandlerLocalProvision,
)
from omnibase_infra.nodes.node_setup_local_provision_effect.handlers.handler_local_status import (
    HandlerLocalStatus,
)
from omnibase_infra.nodes.node_setup_local_provision_effect.models.model_local_provision_effect_output import (
    ModelLocalProvisionEffectOutput,
)

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_stack_requirements import (
    HandlerSetupStackRequirements,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_workflow import (
    _previous_state,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import (
    ModelSetupObservation,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

_OWNER_INVOCATION: ContextVar[object | None] = ContextVar(
    "setup_stack_invocation", default=None
)
_LOG_FIELDS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)


class _InvocationLogFilter(logging.Filter):
    """Hide only this invocation's arbitrary owner payload, not concurrent logs."""

    def __init__(self, invocation: object) -> None:
        super().__init__()
        self._invocation = invocation

    def filter(self, record: logging.LogRecord) -> bool:
        if _OWNER_INVOCATION.get() is self._invocation:
            for key in tuple(record.__dict__):
                if key not in _LOG_FIELDS:
                    del record.__dict__[key]
            record.msg = "Local stack owner operation: %s"
            record.args = (record.levelname,)
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


def _read_input(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4 * 1024 * 1024:
            raise ValueError("Stack input must be a bounded regular file")
        return source.read(4 * 1024 * 1024 + 1)


class HandlerSetupLocalStack:
    """Reconcile only explicitly selected topology services; never tear down."""

    name = "local-stack"

    def __init__(self, *, onex_home: Path | None = None) -> None:
        home = onex_home or user_config_path().parent
        _, previous = _previous_state(home / "setup.json")
        self._applied_digest = (
            next(
                (
                    item.evidence.get("input_digest")
                    for item in previous.observations
                    if item.step == self.name and item.status == "success"
                ),
                None,
            )
            if previous is not None
            else None
        )

    def _inputs(
        self,
        plan: ModelSetupPlan,
    ) -> tuple[dict[str, object], tuple[str, ...], str]:
        selection = plan.local_stack
        if selection is None:
            raise ValueError(
                "Supply explicit compose and topology files for local-stack"
            )
        compose = _read_input(selection.compose_file)
        topology_bytes = _read_input(selection.topology_file)
        topology = ModelDeploymentTopology.model_validate(
            yaml.safe_load(topology_bytes)
        )
        required = tuple(
            sorted(
                {
                    service.local.compose_service
                    for service in topology.local_services().values()
                    if service.local is not None
                }
            )
        )
        if not required:
            raise ValueError(
                "Topology must explicitly select at least one LOCAL service"
            )
        digest = hashlib.sha256(
            selection.model_dump_json().encode()
            + b"\0"
            + compose
            + b"\0"
            + topology_bytes
        ).hexdigest()
        return (
            {
                "topology": topology,
                "compose_file_path": str(selection.compose_file),
                "correlation_id": uuid4(),
                "max_wait_seconds": selection.max_wait_seconds,
                "whole_project": False,
                "build": False,
            },
            required,
            digest,
        )

    @staticmethod
    async def _execute(
        handler: HandlerLocalStatus | HandlerLocalProvision,
        envelope: dict[str, object],
    ) -> ModelLocalProvisionEffectOutput:
        invocation = object()
        token = _OWNER_INVOCATION.set(invocation)
        logger = logging.getLogger(type(handler).__module__)
        log_filter = _InvocationLogFilter(invocation)
        logger.addFilter(log_filter)
        try:
            await handler.initialize({})
            try:
                output = await handler.execute(envelope)
                if not isinstance(output.result, ModelLocalProvisionEffectOutput):
                    raise ValueError("Unexpected local provisioning owner result")
                return output.result
            finally:
                await handler.shutdown()
        finally:
            logger.removeFilter(log_filter)
            _OWNER_INVOCATION.reset(token)

    def observe(self, plan: ModelSetupPlan) -> ModelSetupObservation:
        try:
            envelope, required, digest = self._inputs(plan)
        except (OSError, ValueError, yaml.YAMLError):
            return ModelSetupObservation(
                step=self.name,
                status="action_required",
                fix="Supply readable compose and topology files with at least one LOCAL service; review their schema before retrying.",
            )
        if shutil.which("docker") is None:
            return ModelSetupObservation(
                step=self.name,
                status="action_required",
                fix="The selected local-stack requires an already installed, running Docker with Compose. Endpoint-only setup does not require Docker.",
            )
        requirements = HandlerSetupStackRequirements().observe(plan, required)
        if requirements.status != "success":
            return requirements
        try:
            output = asyncio.run(
                self._execute(HandlerLocalStatus(ModelONEXContainer()), envelope)
            )
            if not output.success:
                return ModelSetupObservation(
                    step=self.name,
                    status="action_required",
                    fix="The existing stack owner could not read Compose status. Check the selected project and Docker access, then retry.",
                )
            complete = set(required).issubset(output.services_running)
            return ModelSetupObservation(
                step=self.name,
                status="success"
                if complete and digest == self._applied_digest
                else "unavailable",
                evidence={
                    **requirements.evidence,
                    "input_digest": digest,
                    "services": ",".join(required),
                    "ownership": "user-supplied stack; disable preserves resources",
                },
                fix=None
                if complete and digest == self._applied_digest
                else "Apply the selected stack configuration with onex setup --action repair.",
            )
        except (OSError, ValueError, RuntimeError):
            return ModelSetupObservation(
                step=self.name,
                status="failure",
                fix="The existing stack owner could not verify the selected services. Check Docker access and the supplied configuration.",
            )

    def apply(self, plan: ModelSetupPlan) -> ModelSetupObservation | None:
        before = self.observe(plan)
        if before.status != "unavailable":
            return before
        envelope, _, digest = self._inputs(plan)
        try:
            result = asyncio.run(
                self._execute(HandlerLocalProvision(ModelONEXContainer()), envelope)
            )
            if result.success:
                self._applied_digest = digest
                return None
        except (OSError, ValueError, RuntimeError):
            pass
        return ModelSetupObservation(
            step=self.name,
            status="failure",
            changed=True,
            fix="The existing stack owner could not start all selected services. Check the supplied project and its service health, then repair; existing resources were retained.",
        )

    def handle(self, plan: ModelSetupPlan) -> ModelSetupObservation:
        if plan.action != "verify":
            result = self.apply(plan)
            if result is not None:
                return result
        return self.observe(plan)
