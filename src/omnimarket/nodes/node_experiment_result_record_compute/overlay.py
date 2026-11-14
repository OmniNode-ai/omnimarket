# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve the experiment result record policy at composition."""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)
from omnimarket.handlers.node_overlay_reader import (
    find_node_overlay,
    load_structured_overlay,
)
from omnimarket.nodes.node_experiment_result_record_compute.models.model_experiment_result_overlay import (
    ModelExperimentResultOverlay,
)
from omnimarket.nodes.node_experiment_result_record_compute.models.model_experiment_result_policy import (
    ModelExperimentResultPolicy,
)

NODE_NAME = "node_experiment_result_record_compute"


def load_policy(
    environ: Mapping[str, str] | None = None,
) -> ModelExperimentResultPolicy:
    """Read the deployment policy or the packaged contract config.

    An invalid selected overlay refuses; it never falls back to the contract values.
    """
    if find_node_overlay(NODE_NAME, environ) is not None:
        return load_structured_overlay(
            NODE_NAME, ModelExperimentResultOverlay, environ
        ).policy
    return ModelExperimentResultPolicy.model_validate(
        load_packaged_contract_section(NODE_NAME, "config")
    )


__all__ = ["NODE_NAME", "load_policy"]
