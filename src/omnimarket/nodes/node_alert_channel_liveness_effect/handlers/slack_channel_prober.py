# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The read-only half of the liveness check (OMN-15600).

This module is the EFFECT boundary: the only place in the node that touches the
network, resolves a secret, or knows that Slack exists.  Everything downstream
of it is a pure function of :class:`ModelAlertChannelObservation`, which is what
lets the hermetic tests drive the shipped classifier instead of a stand-in.

Two Slack Web API methods, both read-only, both asserted on the response
**body**:

``auth.test``
    Is this token still a token?  Answers ``HTTP 200`` with ``{"ok": false,
    "error": "token_revoked"}`` for a revoked one — a status-code check reads
    that as success.

``conversations.info``
    Does the destination channel exist, and is the bot in it?  ``ok:false /
    channel_not_found`` is a dead destination; ``ok:true`` with
    ``channel.is_member == false`` is a destination that will answer
    ``not_in_channel`` to the next real alert.

**Nothing here posts a message.**  A liveness check that writes a canary into
the alert channel every interval trains its readers to ignore the channel, and
a muted channel is exactly the failure this ticket is closing (OMN-14440: fired
every 30 minutes for three months and nobody read it).

No secret value appears in this module, in a log line, or on any model.  The
token is resolved by reference through the canonical secret store and lives only
in the ``Authorization`` header of a request that is never logged.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol

import httpx

from omnimarket.config.service_endpoints import (
    SLACK_AUTH_TEST_URL,
    SLACK_CONVERSATIONS_INFO_URL,
)
from omnimarket.inference.secret_store_resolver import resolve_api_key_loop_safe
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_alert_channel_liveness_effect.models.model_alert_channel_liveness import (
    ModelAlertChannelObservation,
)

logger = logging.getLogger(__name__)

#: Contract-declared secret ref NAMES. The names are read out of the contract
#: via ``contract_secret_ref`` so the mapping lives in one place; the values are
#: never read here and never leave the secret store as anything but a header.
_TOKEN_SECRET = "SLACK_BOT_TOKEN"
_CHANNEL_SECRET = "SLACK_CHANNEL_ID"


class ProtocolAlertChannelProber(Protocol):
    """One read-only observation of the alert channel."""

    def probe(self) -> ModelAlertChannelObservation:
        """Collect the channel's answers without judging them."""
        raise NotImplementedError


class SlackAlertChannelProber:
    """Probe the bot-token alert path with two read-only Web API calls.

    Args:
        contract_path: The node contract carrying the ``secrets`` block whose
            ref names identify the token and the destination channel.
        timeout_seconds: Per-request ceiling, contract-declared. A probe that
            hangs must become PROBE_ERROR on a bounded clock rather than block
            the heartbeat consumer.
    """

    def __init__(self, contract_path: Path, *, timeout_seconds: float) -> None:
        self._contract_path = contract_path
        self._timeout = timeout_seconds

    def probe(self) -> ModelAlertChannelObservation:
        """Return what the channel answered, with no verdict attached.

        Never raises: a failure to observe is itself an observation
        (``transport_error``), and the classifier turns it into PROBE_ERROR.
        Raising here would DLQ a heartbeat onto a malformed-input topic and
        lose the one fact worth keeping — that the channel is unproven.
        """
        token = self._resolve(_TOKEN_SECRET)
        channel = self._resolve(_CHANNEL_SECRET)
        if not token or not channel:
            return ModelAlertChannelObservation(credentials_present=False)

        try:
            with httpx.Client(timeout=self._timeout) as client:
                auth_body = self._call(client, SLACK_AUTH_TEST_URL, token, params=None)
                auth_ok = bool(auth_body.get("ok"))
                if not auth_ok:
                    return ModelAlertChannelObservation(
                        credentials_present=True,
                        auth_ok=False,
                        auth_error=self._error_of(auth_body),
                    )

                channel_body = self._call(
                    client,
                    SLACK_CONVERSATIONS_INFO_URL,
                    token,
                    params={"channel": channel},
                )
        except Exception as exc:
            return ModelAlertChannelObservation(
                credentials_present=True,
                transport_error=f"{type(exc).__name__}: {exc}",
            )

        channel_ok = bool(channel_body.get("ok"))
        if not channel_ok:
            return ModelAlertChannelObservation(
                credentials_present=True,
                auth_ok=True,
                channel_ok=False,
                channel_error=self._error_of(channel_body),
            )

        channel_info = channel_body.get("channel")
        is_member = (
            bool(channel_info.get("is_member"))
            if isinstance(channel_info, dict) and "is_member" in channel_info
            else None
        )
        return ModelAlertChannelObservation(
            credentials_present=True,
            auth_ok=True,
            channel_ok=True,
            bot_is_member=is_member,
        )

    def _call(
        self,
        client: httpx.Client,
        url: str,
        token: str,
        *,
        params: dict[str, str] | None,
    ) -> dict[str, Any]:
        """GET one Slack Web API method and return its parsed body.

        The URL is complete and verbatim from the service-endpoint authority —
        never assembled at runtime, per the bare-base fail-closed rule.
        """
        response = client.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
        body: Any = response.json()
        if not isinstance(body, dict):
            raise ValueError(
                f"Slack answered {url} with a non-object body of type "
                f"{type(body).__name__}; the response cannot be judged"
            )
        return body

    @staticmethod
    def _error_of(body: dict[str, Any]) -> str:
        """The ``error`` code Slack named, as a string."""
        error = body.get("error")
        return str(error) if error else "unknown_error"

    def _resolve(self, secret_name: str) -> str | None:
        """Resolve one contract-declared secret ref, or ``None``.

        ``resolve_api_key_loop_safe`` rather than the plain sync resolver:
        ``handle`` is synchronous and the runtime may dispatch it from inside
        the kernel's own event loop, where the sync-only guard raises
        (OMN-13843).

        ``env_var_fallback`` is threaded for the same reason
        ``node_slack_publish_effect`` threads it (OMN-16778): the deployed lane
        profiles map the DOTTED ``slack.bot_token`` with convention fallback
        OFF, so an unmapped literal ref name gets no source spec and resolves to
        None with the token present in the container the whole time. That
        ref-mapping residual is owned by a separate lane; this call site does
        not touch the mapping surface.
        """
        ref = contract_secret_ref(self._contract_path, secret_name)
        try:
            secret = resolve_api_key_loop_safe(ref, env_var_fallback=ref)
        except Exception as exc:
            logger.warning(
                "alert-channel liveness: configured secret %s did not resolve (%s); "
                "the channel is reported NOT_CONFIGURED rather than assumed "
                "healthy",
                secret_name,
                type(exc).__name__,
            )
            return None
        if secret is None:
            return None
        return secret.get_secret_value()


__all__ = ["ProtocolAlertChannelProber", "SlackAlertChannelProber"]
