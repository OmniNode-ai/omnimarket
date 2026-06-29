# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recorded-from-real-call inference replay fixtures (OMN-13498 B1).

The replay adapter here is NOT a hand-written fake. Each fixture JSON under this
directory was CAPTURED FROM A REAL z.ai GLM (``cloud-glm``) call: the recorder
resolved the ``cloud-glm`` backend through the SAME routing authority the
delegation call path uses (``resolve_delegation_backend`` against the committed
``bifrost_delegation.yaml``), resolved the API key at the effect boundary
(``resolve_api_key``), and posted each node's prompt VERBATIM via the canonical
transport (``post_chat_completion``). The recorded ``raw_response`` is the real
model's output; the replay adapter simply returns it.

LAN boundary note (macOS LAN-grant constraint, per CLAUDE.md): the local vLLM
``.201`` backends are LAN-only and unreachable from a uv-managed interpreter, so
recording used the internet-reachable cloud ``cloud-glm`` backend (``glm-5.2``)
that the routing contract resolves — never a canned/echo fake.

Why this replaces the deleted ``_FakeInferenceBridge`` / ``_FakeInferenceAdapter``
fakes (which the OMN-13497 ``check-no-faked-boundary`` validator flags):

* the deleted fakes accepted ANY ``model_key`` (including a delegation TIER name
  such as ``cheap_cloud``) and returned a hand-authored canned string. That is the
  exact ``feedback_real_dispatch_path_tests`` failure mode — a fake that lets a
  ``Unknown model_key: 'cheap_cloud'`` class of bug ship green.
* this replay adapter HARD-REJECTS a delegation TIER name handed in as a
  ``model_key`` (it raises), so it cannot mask the tier-name-as-model_key
  regression — exactly the validation ``RecordedJudgeReplayAdapter`` (OMN-13470)
  established.
"""

from __future__ import annotations

import json
from pathlib import Path

from omnimarket.inference.adapter_inference_bridge import ModelInferenceAdapter

_FIXTURE_DIR = Path(__file__).resolve().parent

# Delegation TIER names — a tier name reaching the inference layer as a model_key
# is the OMN-13470 bug class; the replay adapter rejects them so a recorded
# replay can never green-light a cheap_cloud-class routing bug.
_DELEGATION_TIER_NAMES = frozenset(
    {"cheap_cloud", "cheap_frontier", "frontier_api", "local", "unknown"}
)


def load_recorded_response(fixture_name: str) -> str:
    """Return the recorded real ``raw_response`` from a replay fixture JSON."""
    record = json.loads((_FIXTURE_DIR / fixture_name).read_text())
    return str(record["raw_response"])


def recorded_model_id(fixture_name: str) -> str:
    """Return the concrete model id the fixture was recorded from."""
    record = json.loads((_FIXTURE_DIR / fixture_name).read_text())
    return str(record["resolved_model_id"])


class RecordedReplayInferenceAdapter(ModelInferenceAdapter):
    """Replay REAL-recorded GLM responses, rejecting any delegation tier name.

    Single-route form: pass ``fixture_name`` and every ``infer`` call replays that
    recorded response. Multi-route form: pass ``route_fixtures`` mapping each
    concrete model route key to a fixture name (used by the fan-out orchestrators).

    Fails closed if handed a delegation TIER name as ``model_key`` — so it can
    never mask the tier-name-as-model_key regression a hand-written fake would.
    """

    def __init__(
        self,
        fixture_name: str | None = None,
        *,
        route_fixtures: dict[str, str] | None = None,
        default_response: str = "[]",
    ) -> None:
        if fixture_name is None and route_fixtures is None:
            raise ValueError("provide fixture_name or route_fixtures")
        self._single = (
            load_recorded_response(fixture_name) if fixture_name is not None else None
        )
        self._routes = {
            key: load_recorded_response(name)
            for key, name in (route_fixtures or {}).items()
        }
        self._default_response = default_response
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
                "the inference layer as a model_key (the OMN-13470 bug class). The "
                "caller must resolve a CONCRETE model route from the routing "
                "authority before dispatching inference."
            )
        self.calls.append({"model_key": model_key, "temperature": temperature})
        if self._single is not None:
            return self._single
        return self._routes.get(model_key, self._default_response)


__all__ = [
    "RecordedReplayInferenceAdapter",
    "load_recorded_response",
    "recorded_model_id",
]
