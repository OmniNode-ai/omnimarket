# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the reviewed list of unregistered nodes from this node's own contract (OMN-24354)."""

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)
from omnimarket.nodes.node_entry_point_registration_compute.models import (
    ModelEntryPointRegistrationConfig,
)

NODE_NAME = "node_entry_point_registration_compute"


def load_registration_config() -> ModelEntryPointRegistrationConfig:
    """Validate the contract's `config` section; a malformed list refuses composition."""
    return ModelEntryPointRegistrationConfig.model_validate(
        load_packaged_contract_section(NODE_NAME, "config")
    )
