# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load the gate's policy: every pattern and list it reads lives in ``policy.json`` beside the node."""

from __future__ import annotations

import json
from pathlib import Path

from omnimarket.nodes.node_landing_proof_gate_compute.models.model_proof_gate import (
    ModelProofGatePolicy,
)

POLICY_FILE = Path(__file__).resolve().parent.parent / "policy.json"


def load_policy(path: Path | None = None) -> ModelProofGatePolicy:
    return ModelProofGatePolicy(**json.loads((path or POLICY_FILE).read_text()))
