# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the callers' pin policy from the compute node's contract (OMN-20134).

The policy is contract data of node_receipt_gate_pin_advance_compute. That node
is pure and reads no file, so the effect step, which may, loads the data and
passes it in the planning request.
"""

from __future__ import annotations

from importlib import resources

import yaml

from omnimarket.nodes.node_receipt_gate_pin_advance_compute.models import (
    ModelReceiptGatePinPolicy,
)

_COMPUTE_PACKAGE = "omnimarket.nodes.node_receipt_gate_pin_advance_compute"
_POLICY_KEY = "receipt_gate_pin_policy"


def load_pin_policy() -> ModelReceiptGatePinPolicy:
    """The ``receipt_gate_pin_policy`` block of the compute node's contract."""
    text = (
        resources.files(_COMPUTE_PACKAGE).joinpath("contract.yaml").read_text("utf-8")
    )
    return ModelReceiptGatePinPolicy.model_validate(yaml.safe_load(text)[_POLICY_KEY])
