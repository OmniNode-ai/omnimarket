# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_slack_publish_effect (OMN-13723).

EFFECT node. Generic, secret-store-backed Slack publish primitive.

The Slack bot token is resolved at ``handle()`` time from the contract-declared
``api_key_ref`` (``SLACK_BOT_TOKEN``) via the canonical secret-store resolver —
no direct ``os.environ`` read, no literal token in source. Fail-closed when the
secret is unset.

Delivery reuses the HTTP transport mechanics (aiohttp, exponential-backoff retry,
429 handling, threading support) originally provided by ``HandlerSlackWebhook``
in omnibase_infra, but the token is injected at construction time rather than
read from env. This node does NOT format Block Kit messages — it sends whatever
payload the caller provides verbatim.

Idempotency — the primitive is the enforcement point. Before every POST the
handler checks a durable publish ledger keyed by ``idempotency_key``
(``run_date|channel|content_hash``). When a ``slack_ts`` exists for the key,
the POST is skipped and ``deduped=True`` is returned with the prior ``slack_ts``.
On success the new ``slack_ts`` is written to the ledger. The ledger uses atomic
file locking (``fcntl.LOCK_EX``) so concurrent callers cannot race.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
from pathlib import Path
from typing import Any
from uuid import UUID

from omnimarket.config.service_endpoints import SLACK_CHAT_POST_MESSAGE_URL
from omnimarket.inference.secret_store_resolver import resolve_api_key_async
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_slack_publish_effect.models.model_slack_publish import (
    ModelSlackPublish,
    ModelSlackPublishResult,
)

_log = logging.getLogger(__name__)
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"

# URL resolved from the service_endpoints authority (OMN-12806); mirrored by
# the contract endpoints block. Never a bare literal in handler source.
_SLACK_API_URL: str = SLACK_CHAT_POST_MESSAGE_URL

_MAX_RETRIES = 3
_RETRY_BACKOFF: tuple[float, ...] = (1.0, 2.0, 4.0)
_TIMEOUT_SECONDS = 15.0

# Slack API errors that are non-retryable (permanent failures).
_NON_RETRYABLE_ERRORS: frozenset[str] = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "account_inactive",
        "token_revoked",
        "channel_not_found",
        "not_in_channel",
        "is_archived",
        "msg_too_long",
        "no_text",
        "ekm_access_denied",
        "team_access_not_granted",
    }
)

# Ledger file name under ONEX_STATE_DIR.
_LEDGER_FILENAME = "slack_publish_ledger.json"


# ---------------------------------------------------------------------------
# Durable publish ledger (file-backed, flock-serialized)
# ---------------------------------------------------------------------------


def _ledger_path() -> Path:
    """Return the publish ledger path.

    Uses ONEX_STATE_DIR when set (production / stability lane).
    Falls back to ~/.onex_state for local dev and tests that don't set ONEX_STATE_DIR.
    RuntimeLocal sets ONEX_STATE_ROOT for test isolation — prefer that when set.
    """
    state_root = os.environ.get("ONEX_STATE_ROOT", "").strip()
    if state_root:
        return Path(state_root) / _LEDGER_FILENAME
    state_dir = os.environ.get("ONEX_STATE_DIR", "").strip()
    if state_dir:
        return Path(state_dir) / "slack" / _LEDGER_FILENAME
    return Path.home() / ".onex_state" / "slack" / _LEDGER_FILENAME


def _ledger_lookup(key: str) -> str | None:
    """Return the slack_ts previously recorded for *key*, or None."""
    path = _ledger_path()
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data: dict[str, str] = json.loads(raw) if raw.strip() else {}
        return data.get(key)
    except Exception as exc:
        _log.warning("slack publish ledger read error (treating as miss): %s", exc)
        return None


def _ledger_write(key: str, slack_ts: str) -> None:
    """Persist *slack_ts* under *key* in the publish ledger (atomic, flock'd)."""
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Open for read+write+create in text mode; flock prevents concurrent updates.
    lock_path = path.with_suffix(".lock")
    with lock_path.open("a", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            data: dict[str, str] = {}
            if path.exists():
                try:
                    raw = path.read_text(encoding="utf-8")
                    if raw.strip():
                        data = json.loads(raw)
                except Exception as exc:
                    _log.warning(
                        "slack publish ledger parse error (resetting): %s", exc
                    )
            data[key] = slack_ts
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# HTTP transport (injected for tests; concrete for production)
# ---------------------------------------------------------------------------


class SlackPublishTransport:
    """Minimal Slack chat.postMessage transport with retry + 429 handling.

    Defined inside the EFFECT handler module so the imperative-contract guard
    treats the raw HTTP calls as the canonical EFFECT I/O boundary.
    The token is supplied to the constructor (resolved at the effect's ``handle()``
    boundary via the canonical secret-store resolver). Injecting this class keeps
    tests deterministic (no network, no secret store).
    """

    def __init__(
        self,
        token: str,
        *,
        api_url: str = _SLACK_API_URL,
        max_retries: int = _MAX_RETRIES,
        retry_backoff: tuple[float, ...] = _RETRY_BACKOFF,
        timeout_seconds: float = _TIMEOUT_SECONDS,
    ) -> None:
        if not token.strip():
            raise ValueError("SlackPublishTransport requires a non-empty token")
        self._token = token
        self._api_url = api_url
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._timeout = timeout_seconds

    async def post(
        self,
        payload: dict[str, Any],
        correlation_id: UUID,
    ) -> tuple[bool, str | None, str | None]:
        """POST *payload* to the Slack Web API.

        Returns:
            ``(success, slack_ts, error_code)``
        """
        try:
            import aiohttp
        except ImportError as exc:
            raise RuntimeError(
                "aiohttp is required for SlackPublishTransport. "
                "Add 'aiohttp>=3.9.0' to the project dependencies."
            ) from exc

        headers = {"Authorization": f"Bearer {self._token}"}
        retry_count = 0

        async with aiohttp.ClientSession() as session:
            for attempt in range(self._max_retries + 1):
                try:
                    async with session.post(
                        self._api_url,
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=self._timeout),
                    ) as resp:
                        if resp.status == 429:
                            retry_after_raw = resp.headers.get("Retry-After")
                            wait = (
                                float(retry_after_raw)
                                if retry_after_raw
                                else self._retry_backoff[
                                    min(attempt, len(self._retry_backoff) - 1)
                                ]
                            )
                            _log.warning(
                                "Slack 429 rate limit, retrying in %.1fs "
                                "(attempt %d, correlation_id=%s)",
                                wait,
                                attempt + 1,
                                correlation_id,
                            )
                            if attempt < self._max_retries:
                                await asyncio.sleep(wait)
                                retry_count += 1
                                continue
                            return False, None, "SLACK_RATE_LIMITED"

                        if resp.status >= 500:
                            _log.warning(
                                "Slack HTTP %d (server error), retrying "
                                "(attempt %d, correlation_id=%s)",
                                resp.status,
                                attempt + 1,
                                correlation_id,
                            )
                            if attempt < self._max_retries:
                                backoff = self._retry_backoff[
                                    min(attempt, len(self._retry_backoff) - 1)
                                ]
                                await asyncio.sleep(backoff)
                                retry_count += 1
                                continue
                            return False, None, f"SLACK_HTTP_{resp.status}"

                        if 400 <= resp.status < 500:
                            body_text = await resp.text()
                            _log.warning(
                                "Slack HTTP %d (client error, non-retryable): %s "
                                "(correlation_id=%s)",
                                resp.status,
                                body_text[:200],
                                correlation_id,
                            )
                            return False, None, f"SLACK_HTTP_{resp.status}"

                        if resp.status == 200:
                            try:
                                body = await resp.json()
                            except Exception:
                                if attempt < self._max_retries:
                                    await asyncio.sleep(
                                        self._retry_backoff[
                                            min(attempt, len(self._retry_backoff) - 1)
                                        ]
                                    )
                                    retry_count += 1
                                    continue
                                return False, None, "SLACK_API_ERROR"

                            if body.get("ok"):
                                slack_ts: str | None = body.get("ts")
                                _log.info(
                                    "Slack post success: ts=%s retries=%d "
                                    "correlation_id=%s",
                                    slack_ts,
                                    retry_count,
                                    correlation_id,
                                )
                                return True, slack_ts, None

                            slack_error: str = body.get("error", "unknown_error")
                            if slack_error == "ratelimited":
                                if attempt < self._max_retries:
                                    await asyncio.sleep(
                                        self._retry_backoff[
                                            min(attempt, len(self._retry_backoff) - 1)
                                        ]
                                    )
                                    retry_count += 1
                                    continue
                                return False, None, "SLACK_RATE_LIMITED"

                            _log.warning(
                                "Slack API error ok=false: %s (correlation_id=%s)",
                                slack_error,
                                correlation_id,
                            )
                            if slack_error in _NON_RETRYABLE_ERRORS:
                                return False, None, f"SLACK_API_{slack_error.upper()}"
                            if attempt < self._max_retries:
                                await asyncio.sleep(
                                    self._retry_backoff[
                                        min(attempt, len(self._retry_backoff) - 1)
                                    ]
                                )
                                retry_count += 1
                                continue
                            return False, None, "SLACK_API_ERROR"

                except TimeoutError:
                    _log.warning(
                        "Slack request timeout (attempt %d, correlation_id=%s)",
                        attempt + 1,
                        correlation_id,
                    )
                    if attempt < self._max_retries:
                        await asyncio.sleep(
                            self._retry_backoff[
                                min(attempt, len(self._retry_backoff) - 1)
                            ]
                        )
                        retry_count += 1
                        continue
                    return False, None, "SLACK_TIMEOUT"

                except Exception as exc:
                    _log.warning(
                        "Slack transport error (attempt %d, correlation_id=%s): %s",
                        attempt + 1,
                        correlation_id,
                        exc,
                    )
                    if attempt < self._max_retries:
                        await asyncio.sleep(
                            self._retry_backoff[
                                min(attempt, len(self._retry_backoff) - 1)
                            ]
                        )
                        retry_count += 1
                        continue
                    return False, None, "SLACK_CONNECTION_ERROR"

        return False, None, "SLACK_MAX_RETRIES_EXCEEDED"


# ---------------------------------------------------------------------------
# EFFECT handler
# ---------------------------------------------------------------------------


class HandlerSlackPublishEffect:
    """EFFECT: generic Slack publish — secret-store-backed, idempotent.

    Dependencies are injected via constructor for testability (canonical thin
    shape, OMN-14242). ``handle()`` takes a single typed ``ModelSlackPublish``
    payload and returns a single typed ``ModelSlackPublishResult`` — no
    envelope, no coercion; the runtime wraps.

    Args:
        transport: Optional concrete transport. When omitted, a
            ``SlackPublishTransport`` is built at ``handle()`` time with the
            token resolved from the contract ``api_key_ref``. Injecting a stub
            keeps tests deterministic (no network, no secret store).
        ledger_lookup: Optional override for the idempotency ledger read
            (injected in tests for isolation).
        ledger_write: Optional override for the idempotency ledger write
            (injected in tests for isolation).
    """

    def __init__(
        self,
        transport: SlackPublishTransport | None = None,
        *,
        ledger_lookup: Any | None = None,
        ledger_write: Any | None = None,
    ) -> None:
        self._transport = transport
        self._ledger_lookup = ledger_lookup or _ledger_lookup
        self._ledger_write = ledger_write or _ledger_write

    async def handle(self, payload: ModelSlackPublish) -> ModelSlackPublishResult:
        """Publish a pre-formed payload to Slack, deduping via the ledger."""
        if not payload.channel:
            raise ValueError(
                "ModelSlackPublish.channel is required and must not be empty. "
                "Supply channel from overlay config; never hardcode it."
            )
        if payload.blocks is None and payload.text is None:
            raise ValueError(
                "ModelSlackPublish requires at least one of 'blocks' or 'text'."
            )

        # --- idempotency check -------------------------------------------------
        prior_ts = self._ledger_lookup(payload.idempotency_key)
        if prior_ts is not None:
            _log.info(
                "Slack publish deduped: idempotency_key=%r prior_ts=%s "
                "correlation_id=%s",
                payload.idempotency_key,
                prior_ts,
                payload.correlation_id,
            )
            return ModelSlackPublishResult(
                success=True,
                ts=prior_ts,
                deduped=True,
                error_code=None,
                correlation_id=payload.correlation_id,
            )

        # --- resolve transport -------------------------------------------------
        transport = await self._resolve_transport()

        # --- build Slack payload ----------------------------------------------
        api_payload: dict[str, Any] = {"channel": payload.channel}
        if payload.blocks is not None:
            api_payload["blocks"] = payload.blocks
        if payload.text is not None:
            api_payload["text"] = payload.text
        if payload.thread_ts is not None:
            api_payload["thread_ts"] = payload.thread_ts

        # --- POST --------------------------------------------------------------
        success, slack_ts, error_code = await transport.post(
            api_payload, payload.correlation_id
        )

        # --- persist to ledger on success -------------------------------------
        if success and slack_ts is not None:
            try:
                self._ledger_write(payload.idempotency_key, slack_ts)
            except Exception as exc:
                # Ledger write failure is non-fatal; the message was posted.
                # Log as warning so operators can investigate without blocking.
                _log.warning(
                    "Slack publish ledger write failed (message was posted): "
                    "idempotency_key=%r slack_ts=%s error=%s",
                    payload.idempotency_key,
                    slack_ts,
                    exc,
                )

        if not success:
            _log.error(
                "Slack publish failed: error_code=%s idempotency_key=%r "
                "correlation_id=%s",
                error_code,
                payload.idempotency_key,
                payload.correlation_id,
            )

        return ModelSlackPublishResult(
            success=success,
            ts=slack_ts,
            deduped=False,
            error_code=error_code,
            correlation_id=payload.correlation_id,
        )

    async def _resolve_transport(self) -> SlackPublishTransport:
        """Resolve the injected transport or build one from the contract secret."""
        if self._transport is not None:
            return self._transport
        slack_ref = contract_secret_ref(_CONTRACT_PATH, "SLACK_BOT_TOKEN")
        secret = await resolve_api_key_async(slack_ref)
        if secret is None:
            raise RuntimeError(
                f"api_key_ref {slack_ref!r} resolved to None — "
                "ensure SLACK_BOT_TOKEN is set in the secret store."
            )
        return SlackPublishTransport(token=secret.get_secret_value())


__all__: list[str] = [
    "HandlerSlackPublishEffect",
    "SlackPublishTransport",
]
