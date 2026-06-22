# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recorded-from-real-call judge inference replay fixtures (OMN-13470).

The replay adapter here is NOT a hand-written fake: it replays a response that
was CAPTURED FROM A REAL z.ai GLM call (``glm_code_adequacy_pass.json``) and it
HARD-VALIDATES that the ``model_key`` it is handed is the concrete resolved model
id (``glm-5.2``) — it raises on a delegation TIER name. That validation is what
makes this replay unable to mask the OMN-13470 bug, unlike the deleted
``_FakeBridge`` which accepted any ``model_key`` (including the tier name
``cheap_cloud``) and so let the live bug ship green.
"""

from __future__ import annotations

import json
from pathlib import Path

from omnimarket.inference.adapter_inference_bridge import ModelInferenceAdapter

_FIXTURE_DIR = Path(__file__).resolve().parent

# Delegation TIER names — a tier name reaching the inference layer as a model_key
# is exactly the OMN-13470 bug; the replay adapter rejects them.
_DELEGATION_TIER_NAMES = frozenset(
    {"cheap_cloud", "cheap_frontier", "frontier_api", "local", "unknown"}
)


class RecordedJudgeReplayAdapter(ModelInferenceAdapter):
    """Replay a REAL-recorded GLM judge response, pinned to the concrete model.

    Fails closed if handed a tier name (or any model_key other than the recorded
    concrete model id), so it can never green-light the tier-name-as-model_key
    regression the live bug shipped on.
    """

    def __init__(self, fixture_name: str = "glm_code_adequacy_pass.json") -> None:
        record = json.loads((_FIXTURE_DIR / fixture_name).read_text())
        self._expected_model_id = str(record["resolved_model_id"])
        self._raw_response = str(record["raw_response"])
        self.calls: list[dict[str, object]] = []

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        temperature: float | None = None,
    ) -> str:
        if model_key in _DELEGATION_TIER_NAMES:
            raise ValueError(
                f"Unknown model_key: {model_key!r} — a delegation TIER name reached "
                "the inference layer as a model_key (the OMN-13470 bug). The judge "
                "must resolve a CONCRETE model id from the routing authority."
            )
        if model_key != self._expected_model_id:
            raise ValueError(
                f"Recorded replay is pinned to concrete model {self._expected_model_id!r}; "
                f"got {model_key!r}. Re-record the fixture if the judge backend changed."
            )
        self.calls.append({"model_key": model_key, "temperature": temperature})
        return self._raw_response


__all__ = ["RecordedJudgeReplayAdapter"]
