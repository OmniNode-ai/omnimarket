# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Proof-class enum for evidence bundle provenance classification.

Promoted from node_on_vs_off_experiment_compute to omnimarket.enums in OMN-12794
(P2-1) because node_generation_consumer also uses it for attempt-reduction rows.
Any node that emits a scored evidence bundle should import from here.
"""

from __future__ import annotations

from enum import StrEnum


class EnumProofClass(StrEnum):
    """Explicit classification of the evidence bundle provenance.

    REPLAY_PROVEN: all token counts sourced from pre-captured fixtures;
        the harness is fully deterministic and can be re-run offline.
    RUNTIME_OBSERVED_ONLY: token counts captured from live LLM inference;
        results depend on model state and cannot be replayed offline.
    """

    REPLAY_PROVEN = "replay-proven"
    RUNTIME_OBSERVED_ONLY = "runtime-observed-only"


__all__ = [
    "EnumProofClass",
]
