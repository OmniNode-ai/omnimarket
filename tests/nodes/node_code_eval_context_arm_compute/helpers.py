# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and answer builders shared by the node_code_eval_context_arm_compute tests (OMN-23629)."""

from __future__ import annotations

from pathlib import Path

import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_failure import EnumContextPackFailure
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus

from omnimarket.models.context_pack_orchestrator import (
    EnumContextPackOrchestratorStatus,
    ModelContextPackBuilt,
    ModelContextPackTierStatus,
)
from omnimarket.models.context_pack_render import ModelContextPackRenderMessage
from omnimarket.nodes.node_code_eval_context_arm_compute.handlers.handler_code_eval_context_arm import (
    HandlerCodeEvalContextArm,
)
from omnimarket.nodes.node_code_eval_context_arm_compute.models.model_code_eval_context_arm import (
    ModelCodeEvalContextArmRequest,
    ModelCodeEvalTask,
    ModelContextArmBackend,
    ModelContextArmOverlay,
)

NODE_NAME = "node_code_eval_context_arm_compute"
OVERLAY_FILE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "overlays"
    / NODE_NAME
    / "overlay.yaml"
)
CORRELATION_ID = "corr-omn23629"
BASE_COMMIT_TIME = "2026-11-01T00:00:00Z"
EXEMPLAR_SHA = "a" * 12
EXEMPLAR_WHEN = "2026-10-01T00:00:00+00:00"
FINAL_PACK_HASH = "sha256:" + "c" * 64


def load_overlay() -> ModelContextArmOverlay:
    """Load and validate the deployment overlay the fixture carries."""
    with OVERLAY_FILE.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return ModelContextArmOverlay.model_validate(data)


def handler() -> HandlerCodeEvalContextArm:
    """A fresh context-arm handler, the contract's own."""
    return HandlerCodeEvalContextArm()


def task(**overrides: str | None) -> ModelCodeEvalTask:
    """One omnimarket compute task at its base commit, any field overridable."""
    values: dict[str, str | None] = {
        "task_id": "omn23629-task-1",
        "repository": "omnimarket",
        "base_commit": "a" * 40,
        "base_commit_time": BASE_COMMIT_TIME,
        "merge_commit": "b" * 40,
        "node_archetype": "compute",
        "lane_kind": None,
    }
    values.update(overrides)
    return ModelCodeEvalTask.model_validate(values)


def backend(composed: int = 1000, ceiling: int = 8169) -> ModelContextArmBackend:
    """The routed backend: a grounded-input ceiling and the request composed without any pack."""
    return ModelContextArmBackend(
        backend_id="backend-omn23629",
        max_grounded_input_tokens=ceiling,
        composed_request_tokens=composed,
        token_estimator="qwen3-tokenizer",
    )


def request(
    operation: str,
    arm: str,
    pack_answer: ModelContextPackBuilt | None = None,
    **task_overrides: str | None,
) -> ModelCodeEvalContextArmRequest:
    """One context-arm request: the fixture overlay and backend, a task with any override."""
    return ModelCodeEvalContextArmRequest(
        operation=operation,
        correlation_id=CORRELATION_ID,
        arm=arm,
        task=task(**task_overrides),
        backend=backend(),
        overlay=load_overlay(),
        pack_answer=pack_answer,
    )


def exemplar_text(sha: str = EXEMPLAR_SHA, when: str = EXEMPLAR_WHEN) -> str:
    """One rendered exemplar block whose merged line names ``sha`` merged at ``when``."""
    return (
        f"[exemplar: document_excerpt] omnimarket#1@{sha}\n"
        "feat: x (#1)\n"
        f"merged {when} as {sha} on origin/dev\n"
        "files (1):\n"
        "A\tf"
    )


def built(
    profile_name: str,
    tiers: tuple[
        tuple[EnumContextFactor, EnumContextSourceStatus, bool, int], ...
    ] = (),
    *,
    excluded_by_budget: tuple[EnumContextFactor, ...] = (),
    status: EnumContextPackOrchestratorStatus = EnumContextPackOrchestratorStatus.OK,
    failure_class: EnumContextPackFailure | None = None,
    exemplar_sha: str = EXEMPLAR_SHA,
    exemplar_when: str = EXEMPLAR_WHEN,
    exemplar_body: str | None = None,
) -> ModelContextPackBuilt:
    """A pack answer built directly: tiers as (factor, status, included, token_estimate).

    The answer carries one rendered exemplar block when an exemplar tier is included;
    ``exemplar_body`` replaces the block verbatim, ``exemplar_sha`` and
    ``exemplar_when`` re-point its merged line.
    """
    carries_exemplar = any(
        factor is EnumContextFactor.EXEMPLAR and included
        for factor, _status, included, _tokens in tiers
    )
    messages = (
        (
            ModelContextPackRenderMessage(
                role="user",
                text=exemplar_body
                if exemplar_body is not None
                else exemplar_text(sha=exemplar_sha, when=exemplar_when),
            ),
        )
        if carries_exemplar
        else ()
    )
    return ModelContextPackBuilt(
        status=status,
        profile_name=profile_name,
        final_pack_hash=FINAL_PACK_HASH,
        messages=messages,
        tier_statuses=tuple(
            ModelContextPackTierStatus(
                factor=factor,
                required=False,
                status=tier_status,
                included=included,
                token_estimate=tokens,
            )
            for factor, tier_status, included, tokens in tiers
        ),
        excluded_by_budget=excluded_by_budget,
        failure_class=failure_class,
    )
