# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bind the scheduled request's deployment template before handling any bus input."""

from importlib.resources import files

import yaml
from omnibase_infra.runtime.overlay.contract_env_ref import expand_contract_env_refs

from omnimarket.models.merge_sweep.model_merge_sweep_run import (
    ModelMergeSweepRunRequest,
)

_CONTRACT = files("omnimarket.nodes.node_merge_sweep_request_compute").joinpath(
    "contract.yaml"
)


def scheduled_request_config() -> tuple[str, ModelMergeSweepRunRequest]:
    """Resolve deployment refs using the runtime's standard overlay boundary."""
    config = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))["config"][
        "scheduled_run"
    ]
    raw = {
        key: expand_contract_env_refs(value) if isinstance(value, str) else value
        for key, value in config["request"].items()
    }
    return str(config["workflow"]), ModelMergeSweepRunRequest.model_validate(raw)
