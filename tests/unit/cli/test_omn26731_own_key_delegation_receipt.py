# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The committed own-key delegation receipt names the customer's model (OMN-26731)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

_RECEIPT = (
    Path(__file__).resolve().parents[3]
    / "contracts/evidence/OMN-26731/ac3-own-key-delegation-receipt.json"
)
_SHA256 = "24492c3235cca11aa1811075266f9958b23dcd3bad8fa6eefece370cbfd3f247"


def test_own_key_delegation_receipt_names_chosen_model_and_customer_key() -> None:
    raw = _RECEIPT.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == _SHA256
    receipt = json.loads(raw)
    assert receipt["terminal_model_used"] == "qwen/qwen3-coder"
    assert receipt["credential_source"] == "customer_key"
    assert receipt["route"] == "byok-openrouter"
    assert receipt["status"] == "completed"
