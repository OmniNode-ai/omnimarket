# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Provider-rung liveness canary (OMN-16987).

Resolvability is not liveness: a key NAME that resolves says nothing about
whether the value authenticates and has quota. Every declared backend of the
bifrost delegation contract that carries an ``endpoint_url`` and a key
reference gets ONE authenticated minimal chat completion and a typed verdict.

* A key reference that does not resolve reads ``UNRESOLVED`` and sends nothing.
* Backends that share endpoint, model and key are one rung on the wire and the
  answer is credited to each, so N probed backends give N verdicts.
* A 429 is classified by the contract's own ``provider_quota_policy`` (z.ai
  ``1310`` reset-window vs ``1113`` unfunded). A ``1310`` that states its reset
  instant suppresses the backend until then: it is not probed again in the
  window and reads ``SKIPPED_SUPPRESSED``.
* Each probed endpoint also receives one deliberately invalid key, which must
  read ``AUTH_DEAD``; a probe that reads 200 whatever it sends measures nothing.
* No resolved value leaves this handler: every string fact is scrubbed of the
  key sent with the request, the result is scrubbed again as a whole, and a
  provider that quotes a key back is counted in ``key_echoes``. The suppression
  state file holds only backend id, reset instant and quota code, scrubbed,
  owner-only: a field containing a resolved value is written as ``<redacted>``.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_delegation_config,
)
from omnimarket.inference.provider_quota_policy import classify_quota_response
from omnimarket.nodes.node_provider_rung_canary_effect.adapters.adapter_provider_rung_post import (
    default_post,
)
from omnimarket.nodes.node_provider_rung_canary_effect.models.model_provider_rung_canary import (
    EnumProviderRungVerdict as V,
)
from omnimarket.nodes.node_provider_rung_canary_effect.models.model_provider_rung_canary import (
    ModelProviderRungCanaryRequest,
    ModelProviderRungCanaryResult,
    ModelProviderRungControl,
    ModelProviderRungSuppression,
    ModelProviderRungVerdict,
)

logger = logging.getLogger(__name__)

ResolveFn = Callable[[str, str | None], Awaitable[str | None]]
# (endpoint_url, payload, headers, timeout_seconds) -> raw facts
PostFn = Callable[[str, dict[str, Any], dict[str, str], float], dict[str, Any]]

PROMPT: Final[str] = "Reply with the single word OK."
MAX_TOKENS: Final[int] = 8
MAX_TIMEOUT_SECONDS: Final[float] = 30.0
# Deliberately invalid and not a secret: its whole purpose is to be refused.
INVALID_KEY: Final[str] = "onex-rung-canary-deliberately-invalid-key-0000"
REDACTED: Final[str] = "<redacted>"
MESSAGE_CAP: Final[int] = 300
# Facts the transport hands back for classification only; never put on a model.
_PRIVATE_FACTS: Final[frozenset[str]] = frozenset({"body", "response_headers"})
_AUTH_STATUSES: Final[frozenset[int]] = frozenset({401, 403})
# The Gemini OpenAI-compatible surface refuses an invalid key with 400
# INVALID_ARGUMENT "Please pass a valid API key", not 401.
_INVALID_KEY_STATUS: Final[str] = "INVALID_ARGUMENT"
_INVALID_KEY_MARKER: Final[str] = "api key"


def backend_secret_ref(backend: Any) -> str | None:
    value = backend.resolved_secret_ref
    return value if isinstance(value, str) and value.strip() else None


def _probe_key(backend: Any) -> str:
    return "|".join(
        [
            str(backend.endpoint_url),
            str(backend.model_name),
            str(backend_secret_ref(backend)),
            str(backend.api_key_env),
        ]
    )


def _payload(model_name: str | None) -> dict[str, Any]:
    return {
        "model": model_name,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": MAX_TOKENS,
        "temperature": 0,
    }


def _headers(extra: dict[str, str] | None, key: str) -> dict[str, str]:
    headers = dict(extra or {})
    headers["Authorization"] = f"Bearer {key}"
    return headers


def default_resolve() -> ResolveFn:
    from omnimarket.inference.secret_store_resolver import (
        resolve_api_key_with_source_async,
    )

    async def resolve(ref: str, env_fallback: str | None) -> str | None:
        value, _source = await resolve_api_key_with_source_async(
            ref, required=False, env_var_fallback=env_fallback
        )
        return value.get_secret_value() if value is not None else None

    return resolve


def classify(fact: dict[str, Any]) -> V:
    """One observed request, one verdict. Pure; the whole grading rule."""
    family = fact.get("exception_family")
    if family == "transport":
        return V.UNREACHABLE
    if family == "decode":
        return V.PROTOCOL_ERROR
    if fact.get("exception") is not None or family is not None:
        return V.PROBE_ERROR
    status = fact.get("http_status")
    if not isinstance(status, int):
        return V.PROBE_ERROR
    if 200 <= status < 300:
        ok = fact.get("body_is_chat_completion") is True and fact.get("model_echo")
        return V.LIVE if ok else V.PROTOCOL_ERROR
    if status in _AUTH_STATUSES:
        return V.AUTH_DEAD
    if (
        status == 400
        and fact.get("provider_error_status") == _INVALID_KEY_STATUS
        and _INVALID_KEY_MARKER in str(fact.get("provider_error_message", "")).lower()
    ):
        return V.AUTH_DEAD
    if status == 429:
        return V.QUOTA_DEAD
    return V.HTTP_ERROR


def _settle(facts: dict[str, Any], sent_key: str) -> tuple[dict[str, Any], int]:
    """Scrub the key that was sent out of every string fact, THEN cap the text."""
    out: dict[str, Any] = {}
    echoes = 0
    for name, value in facts.items():
        if isinstance(value, str) and sent_key:
            echoes += value.count(sent_key)
            value = value.replace(sent_key, REDACTED)
        if name == "provider_error_message" and isinstance(value, str):
            value = value[:MESSAGE_CAP]
        out[name] = value
    return out, echoes


def _load_state(path: str | None) -> list[ModelProviderRungSuppression]:
    if not path or not Path(path).is_file():
        return []
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [ModelProviderRungSuppression.model_validate(r) for r in raw]


def _save_state(
    path: str, rows: tuple[ModelProviderRungSuppression, ...], secrets: set[str]
) -> None:
    safe_rows: list[dict[str, str | None]] = []
    for row in rows:
        fields = {
            "backend_id": row.backend_id,
            "until": row.until.isoformat(),
            "quota_code": row.quota_code,
        }
        for name, value in fields.items():
            if isinstance(value, str) and any(
                secret and secret in value for secret in secrets
            ):
                fields[name] = REDACTED
        safe_rows.append(fields)
    with os.fdopen(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
        "w",
        encoding="utf-8",
    ) as stream:
        stream.write(json.dumps(safe_rows, sort_keys=True))
    os.chmod(path, 0o600)


def _table(rungs: tuple[ModelProviderRungVerdict, ...]) -> str:
    lines = [
        "| backend | tier | verdict | http | detail |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in rungs:
        detail = r.quota_class or r.provider_error_code or r.exception or ""
        if r.suppressed_until is not None:
            detail = f"suppressed until {r.suppressed_until.isoformat()}"
        lines.append(
            f"| {r.backend_id} | {r.tier or ''} | {r.verdict.value} | "
            f"{'' if r.http_status is None else r.http_status} | {detail} |"
        )
    return "\n".join(lines)


class HandlerProviderRungCanary:
    """Probes every credentialed rung of the delegation contract once.

    ``resolve`` and ``post`` are injected: the runtime's own resolver and
    transport in production, fakes in the offline tests.
    """

    def __init__(
        self, resolve: ResolveFn | None = None, post: PostFn | None = None
    ) -> None:
        self._resolve = resolve
        self._post = post or default_post

    async def handle(
        self, request: ModelProviderRungCanaryRequest
    ) -> ModelProviderRungCanaryResult:
        now = request.now or datetime.now(UTC)
        resolve = self._resolve or default_resolve()
        config = load_bifrost_delegation_config(
            config_path=Path(request.contract_path) if request.contract_path else None,
            overlay_path=Path(request.overlay_path) if request.overlay_path else None,
        )
        active = {
            s.backend_id: s
            for s in [*_load_state(request.state_path), *request.suppressions]
            if s.until > now
        }

        secrets: set[str] = set()
        # One probe per distinct rung; the backends behind it share its answer.
        groups: dict[str, list[Any]] = {}
        order: list[str] = []
        for backend in config.backends:
            if not (backend.endpoint_url or "").strip():
                continue
            if backend_secret_ref(backend) is None:
                continue
            key = _probe_key(backend)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(backend)

        facts_by_key: dict[str, dict[str, Any]] = {}
        sent_endpoints: dict[str, tuple[str | None, float, dict[str, str] | None]] = {}
        echoes = 0
        for key in order:
            members = groups[key]
            lead = members[0]
            if all(m.backend_id in active for m in members):
                facts_by_key[key] = {"suppressed": True}
                continue
            ref = backend_secret_ref(lead)
            assert ref is not None
            try:
                value = await resolve(ref, lead.api_key_env)
            except Exception as exc:
                value = None
                facts_by_key[key] = {
                    "resolved": False,
                    "resolver_error": type(exc).__name__,
                }
            if not value:
                facts_by_key.setdefault(key, {"resolved": False})
                continue
            secrets.add(value)
            timeout = (
                min(lead.timeout_ms / 1000.0, MAX_TIMEOUT_SECONDS)
                if lead.timeout_ms and lead.timeout_ms > 0
                else MAX_TIMEOUT_SECONDS
            )
            raw = self._post(
                str(lead.endpoint_url),
                _payload(lead.model_name),
                _headers(lead.extra_headers, value),
                timeout,
            )
            facts, n = _settle(raw, value)
            echoes += n
            facts["resolved"] = True
            facts["request_sent"] = True
            facts_by_key[key] = facts
            sent_endpoints.setdefault(
                str(lead.endpoint_url),
                (lead.model_name, timeout, lead.extra_headers),
            )

        controls: list[ModelProviderRungControl] = []
        if request.wrong_key_control:
            for endpoint, (model, timeout, wire_headers) in sent_endpoints.items():
                raw = self._post(
                    endpoint,
                    _payload(model),
                    _headers(wire_headers, INVALID_KEY),
                    timeout,
                )
                facts, _ = _settle(raw, INVALID_KEY)
                controls.append(
                    ModelProviderRungControl(
                        endpoint_host=urlsplit(endpoint).hostname,
                        verdict=classify(facts),
                        http_status=facts.get("http_status"),
                    )
                )

        rungs: list[ModelProviderRungVerdict] = []
        carried: dict[str, ModelProviderRungSuppression] = dict(active)
        probe_of = {m.backend_id: k for k, ms in groups.items() for m in ms}
        for backend in config.backends:
            base: dict[str, Any] = {
                "backend_id": backend.backend_id,
                "provider": backend.provider,
                "tier": backend.tier,
                "model_name": backend.model_name,
                "secret_ref": backend_secret_ref(backend),
            }
            if not (backend.endpoint_url or "").strip():
                rungs.append(
                    ModelProviderRungVerdict(**base, verdict=V.SKIPPED_NO_ENDPOINT)
                )
                continue
            if backend_secret_ref(backend) is None:
                rungs.append(
                    ModelProviderRungVerdict(**base, verdict=V.SKIPPED_NO_SECRET_REF)
                )
                continue
            key = probe_of[backend.backend_id]
            fact = facts_by_key[key]
            host = urlsplit(str(backend.endpoint_url)).hostname
            shared = tuple(
                m.backend_id for m in groups[key] if m.backend_id != backend.backend_id
            )
            if fact.get("suppressed"):
                rungs.append(
                    ModelProviderRungVerdict(
                        **base,
                        verdict=V.SKIPPED_SUPPRESSED,
                        endpoint_host=host,
                        suppressed_until=active[backend.backend_id].until,
                        shared_probe_with=shared,
                    )
                )
                continue
            if fact.get("resolved") is False:
                rungs.append(
                    ModelProviderRungVerdict(
                        **base,
                        verdict=V.UNRESOLVED,
                        probed=True,
                        endpoint_host=host,
                        exception=fact.get("resolver_error"),
                        shared_probe_with=shared,
                    )
                )
                continue
            verdict = classify(fact)
            quota_fields: dict[str, Any] = {}
            if verdict is V.QUOTA_DEAD:
                quota = classify_quota_response(
                    status_code=429,
                    endpoint_url=str(backend.endpoint_url),
                    body=fact.get("body"),
                    now=now,
                    headers=fact.get("response_headers"),
                )
                if quota is not None:
                    quota_fields["quota_class"] = quota.disposition.value
                    if quota.disabled_until is not None:
                        quota_fields["suppressed_until"] = quota.disabled_until
                        carried[backend.backend_id] = ModelProviderRungSuppression(
                            backend_id=backend.backend_id,
                            until=quota.disabled_until,
                            quota_code=quota.provider_code,
                        )
            rungs.append(
                ModelProviderRungVerdict(
                    **base,
                    verdict=verdict,
                    probed=True,
                    request_sent=True,
                    endpoint_host=host,
                    http_status=fact.get("http_status"),
                    latency_ms=fact.get("latency_ms"),
                    provider_error_code=fact.get("provider_error_code"),
                    provider_error_message=fact.get("provider_error_message"),
                    exception=fact.get("exception"),
                    shared_probe_with=shared,
                    **quota_fields,
                )
            )
            logger.info(
                "provider-rung-canary backend=%s verdict=%s",
                backend.backend_id,
                verdict,
            )

        result_rungs = tuple(rungs)
        result = ModelProviderRungCanaryResult(
            as_of=now,
            rungs=result_rungs,
            controls=tuple(controls),
            suppressions=tuple(sorted(carried.values(), key=lambda s: s.backend_id)),
            key_echoes=echoes,
            summary_table=_table(result_rungs),
        )
        result = _scrub_result(result, secrets)
        if request.state_path:
            _save_state(request.state_path, result.suppressions, secrets)
        return result


def _scrub_result(
    result: ModelProviderRungCanaryResult, secrets: set[str]
) -> ModelProviderRungCanaryResult:
    """Belt and braces: no resolved value survives anywhere in the result."""
    text = result.model_dump_json()
    extra = 0
    for value in sorted(secrets, key=len, reverse=True):
        if value and value in text:
            extra += text.count(value)
            text = text.replace(value, REDACTED)
    if not extra:
        return result
    scrubbed = ModelProviderRungCanaryResult.model_validate_json(text)
    return scrubbed.model_copy(update={"key_echoes": scrubbed.key_echoes + extra})


__all__ = ["HandlerProviderRungCanary", "PostFn", "ResolveFn", "classify"]
