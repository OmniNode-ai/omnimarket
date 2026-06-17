# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_hostile_reviewer inference-bridge factory.

The bridge classes (``AdapterInferenceBridge`` / ``ModelInferenceAdapter`` /
``ModelInferenceBridgeConfig``) were re-homed to the canonical owner package
``omnimarket.inference`` in OMN-13208 (A1). This module retains only the
node-specific ``build_from_contract`` factory, which resolves the node's
contract ``model_routing`` policy into a wired ``AdapterInferenceBridge``.

The node-internal callers and tests import the bridge classes from here for
continuity; the B1 rebuild (OMN-13210) deletes this node and its factory in
favor of the canonical inference effect node.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.inference.adapter_inference_bridge import (
    AdapterInferenceBridge,
    ModelInferenceAdapter,
    ModelInferenceBridgeConfig,
)


def build_from_contract(
    requested_keys: list[str] | None = None,
    runtime_model_configs: Mapping[str, Mapping[str, object]] | None = None,
) -> AdapterInferenceBridge:
    """Build an AdapterInferenceBridge from logical route keys and runtime configs.

    The node contract owns the policy schema. Concrete route configs must be
    supplied by the caller or via the contract-declared JSON env var. Missing
    requested keys or incomplete route configs raise ValueError.

    Args:
        requested_keys: Required logical route keys to load.
        runtime_model_configs: Optional runtime configs keyed by logical route key.

    Returns:
        AdapterInferenceBridge wired with requested model configs.
    """
    from omnimarket.nodes.node_hostile_reviewer.handlers.model_config_loader import (
        build_model_configs,
    )

    configs = build_model_configs(
        requested_keys=requested_keys,
        runtime_model_configs=runtime_model_configs,
    )
    return AdapterInferenceBridge(ModelInferenceBridgeConfig(model_configs=configs))


__all__: list[str] = [
    "AdapterInferenceBridge",
    "ModelInferenceAdapter",
    "ModelInferenceBridgeConfig",
    "build_from_contract",
]
