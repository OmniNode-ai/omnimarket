# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# unimported-handler-allow: HandlerValidatorWorkflowRuntime is resolved at boot from handler_routing in contract.yaml, by module path, not by a Python import
"""The runtime-constructible entry of node_validator_workflow_orchestrator (OMN-25055).

``ValidatorWorkflowPipeline`` takes its overlay, reader, contracts, reducer, emitter and gate as
constructor parameters, so the runtime cannot build it. This handler takes none: on its first
request it loads the two node overlays from ``ONEX_SKILL_OVERLAY_ROOTS`` (no key has a default,
a missing overlay raises), wires the workflow through ``build_workflow`` and runs it. The
verdict reducer runs in process inside the workflow, as its contract says.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

from omnimarket.nodes.node_validator_verdict_reducer.overlay import (
    load_overlay as load_reducer_overlay,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.adapters.two_backend import (
    ValidatorWorkflow,
    build_workflow,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.models.model_validator_workflow import (
    ModelValidatorWorkflowRequest,
    ModelValidatorWorkflowResult,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.overlay import (
    load_overlay as load_workflow_overlay,
)

log = logging.getLogger(__name__)


class HandlerValidatorWorkflowRuntime:
    def __init__(
        self,
        workflow: ValidatorWorkflow | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._workflow = workflow
        self._environ: Mapping[str, str] = (
            environ if environ is not None else os.environ
        )

    def _built(self) -> ValidatorWorkflow:
        if self._workflow is None:
            self._workflow = build_workflow(
                load_workflow_overlay(self._environ),
                load_reducer_overlay(self._environ),
                self._environ,
            )
        return self._workflow

    def handle(
        self, request: ModelValidatorWorkflowRequest
    ) -> ModelValidatorWorkflowResult:
        result = self._built().handler.handle(request)
        log.info(
            "node_validator_workflow_orchestrator handled repo=%s pr=%d head=%s status=%s "
            "(node_validator_verdict_reducer runs in process)",
            request.repo,
            request.pr,
            request.head,
            result.status,
        )
        return result
