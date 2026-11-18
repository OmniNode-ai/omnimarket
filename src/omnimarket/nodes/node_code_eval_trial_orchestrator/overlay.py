# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load the runner overlay of the code eval trial from the node's overlay."""

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.code_eval_trial.model_code_eval_overlay import (
    ModelCodeEvalRunnerOverlay,
)

NODE_NAME = "node_code_eval_trial_orchestrator"


def load_runner_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelCodeEvalRunnerOverlay:
    """Read ``<root>/node_code_eval_trial_orchestrator/overlay.yaml`` under ``ONEX_SKILL_OVERLAY_ROOTS``.

    The package ships ``overlay.example.yaml`` with synthetic values. No key has a default: an absent
    key refuses the load, and an invalid file names its fields.
    """
    return load_structured_overlay(NODE_NAME, ModelCodeEvalRunnerOverlay, environ)


__all__ = ["NODE_NAME", "load_runner_overlay"]
