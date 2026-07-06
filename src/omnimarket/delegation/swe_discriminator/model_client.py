# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Thin OpenAI-compatible chat client for the SWE-discriminator smoke run.

Endpoints are resolved from the COMMITTED graded_ladder routing config
(``ladder_rungs.yaml``) — the same integration catalog the ladder recorder uses
— so no URL literal or ``*_URL`` env read lives in this source (the url-authority
gate). Selection is by rung id:

* ``frontier``      — the cloud rung (``rung_cloud_glm`` by default). Its public
                      ``endpoint_url`` comes from the committed config; the
                      Bearer key is supplied by typed runtime config under the
                      env-name declared by the rung.
* ``cost_routed``   — the local rung (``rung_5090_coder`` by default). Its
                      site-specific endpoint is resolved from typed runtime
                      config or the operator-local bifrost overlay — no host/IP
                      in source (CLAUDE.md rule #6).

Which rung backs each tier is overridable by RUNG ID in runtime config, never by
a hardcoded URL. Missing config fails fast so a silent wrong default cannot
flatter a tier.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from omnimarket.delegation.graded_ladder.harness import load_rungs
from omnimarket.delegation.graded_ladder.models import ModelLadderRung
from omnimarket.delegation.swe_discriminator.models import (
    EnumRouting,
    ModelCall,
    ModelSweDiscriminatorRuntimeConfig,
)

_OVERLAY_PATH = Path.home() / ".omninode" / "delegation" / "bifrost_overrides.yaml"

# Default rung ids backing each tier. The rungs (endpoints, keys) live in the
# committed graded_ladder routing config.
_DEFAULT_FRONTIER_RUNG = "rung_cloud_glm"
_DEFAULT_COST_RUNG = "rung_5090_coder"

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


def _rung_by_id(rung_id: str) -> ModelLadderRung:
    for rung in load_rungs():
        if rung.rung_id == rung_id:
            return rung
    raise RuntimeError(f"rung {rung_id!r} not found in ladder_rungs.yaml")


def _resolve_rung_endpoint(
    rung: ModelLadderRung, runtime_config: ModelSweDiscriminatorRuntimeConfig
) -> str | None:
    """Endpoint for a rung from committed config, runtime config, or overlay."""

    if rung.endpoint_url:
        return rung.endpoint_url
    if rung.endpoint_url_env:
        configured = runtime_config.endpoint_urls_by_env.get(rung.endpoint_url_env, "")
        if configured:
            return configured
    configured = runtime_config.endpoint_urls_by_backend_id.get(rung.backend_id, "")
    if configured:
        return configured
    return _overlay_endpoint(rung.backend_id)


def resolve_tier(
    tier: EnumRouting,
    runtime_config: ModelSweDiscriminatorRuntimeConfig | None = None,
) -> tuple[str, str, str, dict[str, str], str]:
    """Return (endpoint_url, model_name, endpoint_label, headers, tier_str).

    Fails fast (RuntimeError) if a tier's endpoint or key cannot be resolved —
    a silent fallback would let a tier masquerade as run when it was never
    reachable.
    """

    config = runtime_config or ModelSweDiscriminatorRuntimeConfig()
    headers = {"Content-Type": "application/json"}
    if tier is EnumRouting.FRONTIER:
        rung = _rung_by_id(config.frontier_rung_id or _DEFAULT_FRONTIER_RUNG)
        url = _resolve_rung_endpoint(rung, config)
        if not url:
            raise RuntimeError(f"frontier rung {rung.rung_id!r} has no endpoint")
        if rung.api_key_env:
            key = config.api_keys_by_env.get(rung.api_key_env, "")
            if not key:
                raise RuntimeError(
                    f"frontier rung needs runtime config for {rung.api_key_env}"
                )
            headers["Authorization"] = f"Bearer {key}"
        headers.update(rung.extra_headers)
        return url, rung.model_name, f"frontier:{rung.model_name}", headers, "frontier"

    rung = _rung_by_id(config.cost_rung_id or _DEFAULT_COST_RUNG)
    url = _resolve_rung_endpoint(rung, config)
    if not url:
        raise RuntimeError(
            f"cost_routed rung {rung.rung_id!r} has no endpoint "
            f"(configure {rung.endpoint_url_env or rung.backend_id} or the overlay)"
        )
    model = config.model_names_by_backend_id.get(
        rung.backend_id, _overlay_model(rung.backend_id, rung.model_name)
    )
    return url, model, f"cost_routed:{model}", headers, "cost_routed"


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
# the code survives the scratchpad; runtime config may override it per run.
_DEFAULT_MAX_TOKENS = 16384


def chat(
    tier: EnumRouting,
    prompt: str,
    *,
    role: str,
    runtime_config: ModelSweDiscriminatorRuntimeConfig | None = None,
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

    config = runtime_config or ModelSweDiscriminatorRuntimeConfig()
    retries = config.max_retries if runtime_config else retries
    max_tokens = config.max_tokens if runtime_config else max_tokens
    url, model_name, label, headers, tier_str = resolve_tier(tier, config)
    # A throttled frontier tier can otherwise hang the battery (retries*backoff
    # per cell); max_retries caps it so a sustained 429 fast-fails to a blocked
    # cell instead of stalling the run.
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
    choices = body.get("choices") or []
    if not choices:
        return ModelCall(
            role=role,
            tier=tier_str,
            model_name=model_name,
            endpoint_label=label,
            prompt_chars=len(prompt),
            content="",
            latency_ms=latency_ms,
            http_status=status,
            error="malformed response: empty 'choices'",
        )
    choice = choices[0]
    choice = choice if isinstance(choice, dict) else {}
    message = choice.get("message", {})
    content = message.get("content") or ""
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
