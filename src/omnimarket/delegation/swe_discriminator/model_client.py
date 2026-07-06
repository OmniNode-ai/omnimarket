# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Thin OpenAI-compatible chat client for the SWE-discriminator smoke run.

Two tiers back the 2x2 routing axis:

* ``frontier``      — GLM-5.2 via z.ai direct (public URL committed; Bearer key
                      from ``LLM_GLM_API_KEY`` at call time, never committed).
* ``cost_routed``   — the local ladder floor (AI-PC 35B), resolved from the
                      bifrost overlay (``~/.omninode/delegation/bifrost_overrides.yaml``)
                      or the ``BIFROST_LOCAL_CODER_ENDPOINT_URL`` env var.

No host/IP is embedded in source (CLAUDE.md rule #6): the local endpoint is
resolved from the operator-local overlay/env at call time. Missing config
fails fast so a silent wrong-default cannot flatter a cost-routed arm.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from omnimarket.delegation.swe_discriminator.models import EnumRouting, ModelCall

_OVERLAY_PATH = Path.home() / ".omninode" / "delegation" / "bifrost_overrides.yaml"

# Public cloud URL — safe to commit (transcribed from ladder_rungs.yaml).
_FRONTIER_URL = "https://api.z.ai/api/coding/paas/v4/chat/completions"
_FRONTIER_MODEL = "glm-5.2"
_FRONTIER_KEY_ENV = "LLM_GLM_API_KEY"

_LOCAL_BACKEND_ID = "local-coder"
_LOCAL_ENDPOINT_ENV = "BIFROST_LOCAL_CODER_ENDPOINT_URL"

# Rough public list price for GLM (z.ai coding plan), USD per 1M tokens. The
# cost axis of the experiment; local compute is amortized (reported separately,
# not a per-token invoice) so cost_routed cells are NOT flattered by a $0.
_FRONTIER_PROMPT_RATE = 0.60 / 1_000_000
_FRONTIER_COMPLETION_RATE = 2.20 / 1_000_000
# Amortized local compute rate (pre-registered placeholder; compute, not invoice).
_LOCAL_PROMPT_RATE = 0.05 / 1_000_000
_LOCAL_COMPLETION_RATE = 0.05 / 1_000_000


def _overlay_endpoint(backend_id: str) -> str | None:
    if not _OVERLAY_PATH.exists():
        return None
    raw = yaml.safe_load(_OVERLAY_PATH.read_text()) or {}
    for backend in raw.get("backends", []) or []:
        if backend.get("backend_id") == backend_id and backend.get("endpoint_url"):
            return str(backend["endpoint_url"])
    return None


def _overlay_model(backend_id: str, default: str) -> str:
    if not _OVERLAY_PATH.exists():
        return default
    raw = yaml.safe_load(_OVERLAY_PATH.read_text()) or {}
    for backend in raw.get("backends", []) or []:
        if backend.get("backend_id") == backend_id and backend.get("model_name"):
            return str(backend["model_name"])
    return default


def resolve_tier(tier: EnumRouting) -> tuple[str, str, str, dict[str, str], str]:
    """Return (endpoint_url, model_name, endpoint_label, headers, tier_str).

    Fails fast (RuntimeError) if a tier's endpoint or key cannot be resolved —
    a silent fallback would let a cost-routed arm masquerade as run when it was
    never reachable.
    """

    headers = {"Content-Type": "application/json"}
    if tier is EnumRouting.FRONTIER:
        # The frontier endpoint is env-overridable so the same harness can pin a
        # different frontier provider (GLM z.ai default; OpenRouter free coder as
        # a fallback when the shared GLM key is quota-throttled). Public URL only.
        url = os.environ.get("SWE_FRONTIER_URL", _FRONTIER_URL)
        model = os.environ.get("SWE_FRONTIER_MODEL", _FRONTIER_MODEL)
        key_env = os.environ.get("SWE_FRONTIER_KEY_ENV", _FRONTIER_KEY_ENV)
        extra = os.environ.get("SWE_FRONTIER_HEADERS", "")
        key = os.environ.get(key_env, "")
        if not key:
            raise RuntimeError(f"frontier tier needs {key_env} in env")
        headers["Authorization"] = f"Bearer {key}"
        for pair in extra.split(","):
            if ":" in pair:
                hk, hv = pair.split(":", 1)
                headers[hk.strip()] = hv.strip()
        return url, model, f"frontier:{model}", headers, "frontier"

    local_url = os.environ.get(_LOCAL_ENDPOINT_ENV) or _overlay_endpoint(
        _LOCAL_BACKEND_ID
    )
    if not local_url:
        raise RuntimeError(
            f"cost_routed tier needs {_LOCAL_ENDPOINT_ENV} or overlay "
            f"backend {_LOCAL_BACKEND_ID!r} at {_OVERLAY_PATH}"
        )
    local_model = _overlay_model(_LOCAL_BACKEND_ID, "Qwen3.6-35B-A3B")
    return local_url, local_model, f"cost_routed:{local_model}", headers, "cost_routed"


def _rates(tier: EnumRouting) -> tuple[float, float]:
    if tier is EnumRouting.FRONTIER:
        return _FRONTIER_PROMPT_RATE, _FRONTIER_COMPLETION_RATE
    return _LOCAL_PROMPT_RATE, _LOCAL_COMPLETION_RATE


def is_infra_block(call: ModelCall) -> bool:
    """True when a failed call is an infra-availability block (rate-limit /
    endpoint unreachable), NOT a capability failure. Such cells are excluded
    from capability scoring (graded_ladder ``blocked`` convention) so a
    same-session 429 cannot manufacture a false capability regression."""

    return bool(call.error) and (call.http_status in (429, 0, 503, 502))


# Reasoning-aware default budget: a local reasoning model (Qwen MTP / DS-R1
# distill) spends completion tokens on a <think> scratchpad BEFORE the code, so a
# 4096 cap truncates the answer to reasoning prose with no code block on a hard
# task (the OMN-13335 hazard, live-reproduced). The default ceiling is raised so
# the code survives the scratchpad; SWE_MAX_TOKENS overrides per run.
_DEFAULT_MAX_TOKENS = 16384


def chat(
    tier: EnumRouting,
    prompt: str,
    *,
    role: str,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    timeout_s: float = 300.0,
    retries: int = 4,
) -> ModelCall:
    """Issue one chat completion; capture content, tokens, latency, cost.

    Errors are captured into the returned ModelCall (never raised) so one bad
    cell does not wedge the battery — the OMN-12792 zero-rows failure mode.
    Retries on 429/transient with backoff (frontier free/coding tiers throttle).
    ``finish_reason`` is captured so a token-cap truncation is distinguishable
    from a genuine wrong answer.
    """

    url, model_name, label, headers, tier_str = resolve_tier(tier)
    # A throttled frontier tier can otherwise hang the battery (retries*backoff
    # per cell); SWE_MAX_RETRIES caps it so a sustained 429 fast-fails to a
    # blocked cell instead of stalling the run.
    env_retries = os.environ.get("SWE_MAX_RETRIES")
    if env_retries and env_retries.isdigit():
        retries = max(1, int(env_retries))
    env_max_tokens = os.environ.get("SWE_MAX_TOKENS")
    if env_max_tokens and env_max_tokens.isdigit():
        max_tokens = max(256, int(env_max_tokens))
    payload = json.dumps(
        {
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": max_tokens,
        }
    ).encode()
    start = time.monotonic()
    last_exc: Exception | None = None
    body: dict[str, Any] = {}
    status = 0
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                body = json.loads(resp.read().decode())
                status = resp.status
            last_exc = None
            break
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
            last_exc = exc
            code = getattr(exc, "code", None)
            backoff = 20.0 if code == 429 else 3.0
            if attempt < retries:
                time.sleep(backoff)
    if last_exc is not None:
        return ModelCall(
            role=role,
            tier=tier_str,
            model_name=model_name,
            endpoint_label=label,
            prompt_chars=len(prompt),
            content="",
            latency_ms=int((time.monotonic() - start) * 1000),
            http_status=getattr(last_exc, "code", 0) or 0,
            error=str(last_exc),
        )
    latency_ms = int((time.monotonic() - start) * 1000)
    choice = body["choices"][0]
    content = choice["message"].get("content") or ""
    finish_reason = str(choice.get("finish_reason") or "")
    usage = body.get("usage", {}) or {}
    p_tok = int(usage.get("prompt_tokens", 0))
    c_tok = int(usage.get("completion_tokens", 0))
    p_rate, c_rate = _rates(tier)
    return ModelCall(
        role=role,
        tier=tier_str,
        model_name=model_name,
        endpoint_label=label,
        prompt_chars=len(prompt),
        content=content,
        prompt_tokens=p_tok,
        completion_tokens=c_tok,
        latency_ms=latency_ms,
        http_status=status,
        finish_reason=finish_reason,
        cost_usd=round(p_tok * p_rate + c_tok * c_rate, 8),
    )
