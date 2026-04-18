# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_ci_fix_effect [OMN-8993].

EFFECT node. Receives ModelCiFixCommand, returns CiFixResult via ModelHandlerOutput.
Model routing: primary=deepseek-r1-14b, fallback=qwen3-coder-30b per contract.yaml.
ci_override retains deepseek-r1-14b in CI (reasoning model required for diagnosis).
Real LLM + patch logic wired in OMN-8994 (Wave 2).
"""

from __future__ import annotations

import logging
import time
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.nodes.node_ci_fix_effect.models.model_ci_fix_command import (
    ModelCiFixCommand,
)
from omnimarket.nodes.node_ci_fix_effect.models.model_ci_fix_result import CiFixResult

_log = logging.getLogger(__name__)


class HandlerCiFixEffect:
    """EFFECT: diagnose failing CI job via LLM, apply patch, run test gate."""

    async def handle(self, request: ModelCiFixCommand) -> ModelHandlerOutput:  # type: ignore[type-arg]
        """Attempt CI fix. Returns is_noop=True; real LLM wiring lands in OMN-8994."""  # stub-ok
        t0 = time.monotonic()
        _log.info(
            "CI fix attempt (scaffold): %s#%s job=%r run=%s",
            request.repo,
            request.pr_number,
            request.failing_job_name,
            request.run_id_github,
        )
        elapsed = time.monotonic() - t0

        result = CiFixResult(
            pr_number=request.pr_number,
            repo=request.repo,
            run_id_github=request.run_id_github,
            failing_job_name=request.failing_job_name,
            correlation_id=request.correlation_id,
            patch_applied=False,
            local_tests_passed=False,
            is_noop=True,
            elapsed_seconds=elapsed,
        )
        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=request.correlation_id,
            handler_id="node_ci_fix_effect",
            events=(result,),
        )
