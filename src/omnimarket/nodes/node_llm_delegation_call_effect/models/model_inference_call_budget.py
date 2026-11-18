# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The single token a timed-out provider call is logged by.

The ceiling itself is an overlay value read by
``delegation_call_budget_reader`` (OMN-24039).
"""

from __future__ import annotations

from typing import Final

# The single token a timed-out rung is grepped by, in one pass over
# ``docker logs omninode-runtime-effects``.
INFERENCE_TIMEOUT_LOG_TOKEN: Final[str] = "INFERENCE_CALL_TIMEOUT"

__all__ = ["INFERENCE_TIMEOUT_LOG_TOKEN"]
