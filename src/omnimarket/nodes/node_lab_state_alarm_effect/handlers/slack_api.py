# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Slack side of the lab-state alarm (OMN-19998, 17a): one post and one read-back, with injected dependencies.

The alarm goes to the channel the existing alert path already uses (the one ``alert_channel_send`` posts to). Which
secrets hold the bot token and the channel id is overlay data; the values are resolved here, at the effect boundary,
and appear in no model, log line, exception text or receipt.

A post is a ``chat.postMessage`` whose response is checked: HTTP 200 with ``"ok": false`` is a refusal, never a
delivery. The read-back is ``conversations.history`` on the returned timestamp (that one message, inclusive), so
"delivered" means the channel itself returns the message.

Credential resolution mirrors the shared alert-channel library's alarm class: the process environment first, then the
one named key read (never sourced) from the operator's bootstrap env file whose path an environment variable names.
A credential that cannot be resolved is a typed refusal, so the alarm goes to the second channel.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

from omnimarket.nodes.node_lab_state_alarm_effect.handlers.channels import (
    AlarmSendRefusedError,
    ProtocolSlackApi,
)


class ProtocolCredentialSource(Protocol):
    def get(self, name: str) -> str | None: ...


def read_named_key(path: Path, key: str) -> str | None:
    """The value of the last assignment of ``key`` in a ``KEY=VALUE`` file, read by name and never sourced.

    The same rules as the shared alert-channel library: an optional ``export `` prefix, one layer of matching quotes
    removed, comments and blank lines skipped.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    last: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        name, _, value = line.partition("=")
        if name.strip() != key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        last = value
    return last or None


class OperatorEnvCredentials:
    """The process environment, then the named key of the operator's bootstrap env file."""

    def __init__(self, environ: Mapping[str, str], env_file_env: str | None) -> None:
        self._environ = environ
        self._env_file_env = env_file_env

    def get(self, name: str) -> str | None:
        value = self._environ.get(name, "").strip()
        if value:
            return value
        if not self._env_file_env:
            return None
        path = self._environ.get(self._env_file_env, "").strip()
        return read_named_key(Path(path), name) if path else None


Http = Callable[[str, bytes, Mapping[str, str], int], tuple[int, bytes]]


def urllib_http(
    url: str, body: bytes, headers: Mapping[str, str], timeout_s: int
) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url, data=body, headers=dict(headers), method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class SlackWebApi:
    """``chat.postMessage`` and ``conversations.history`` with the response read, over an injected HTTP function."""

    def __init__(
        self,
        *,
        credentials: ProtocolCredentialSource,
        token_ref: str,
        channel_ref: str,
        timeout_s: int,
        http: Http = urllib_http,
    ) -> None:
        self._credentials = credentials
        self._token_ref = token_ref
        self._channel_ref = channel_ref
        self._timeout_s = timeout_s
        self._http = http

    def _resolve(self) -> tuple[str, str]:
        token = self._credentials.get(self._token_ref)
        channel = self._credentials.get(self._channel_ref)
        missing = [
            name
            for name, value in ((self._token_ref, token), (self._channel_ref, channel))
            if not value
        ]
        if missing or token is None or channel is None:
            raise AlarmSendRefusedError(
                f"the Slack credential {' and '.join(missing)} is not resolvable here"
            )
        return token, channel

    def _call(
        self,
        url: str,
        payload: Mapping[str, object],
        token: str,
        *,
        form: bool = False,
    ) -> dict[str, object]:
        # A write method takes a JSON body. A read method (conversations.history) takes form fields and ignores a JSON
        # body, so the read-back would ask for a channel with no arguments and answer with the wrong messages.
        if form:
            content_type = "application/x-www-form-urlencoded"
            body = urllib.parse.urlencode(
                {
                    k: str(v).lower() if isinstance(v, bool) else str(v)
                    for k, v in payload.items()
                }
            ).encode("utf-8")
        else:
            content_type = "application/json; charset=utf-8"
            body = json.dumps(payload).encode("utf-8")
        headers = {"Authorization": f"Bearer {token}", "Content-Type": content_type}
        try:
            status, raw = self._http(url, body, headers, self._timeout_s)
        except (OSError, ValueError) as exc:
            raise AlarmSendRefusedError(
                f"Slack did not answer ({type(exc).__name__})"
            ) from exc
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise AlarmSendRefusedError(
                f"Slack answered HTTP {status} with no JSON"
            ) from exc
        if status != 200 or not isinstance(body, dict):
            raise AlarmSendRefusedError(f"Slack answered HTTP {status}")
        return body

    def post(self, text: str) -> str:
        # Imported where used: the endpoint authority pulls in the settings package, which the overlay check and the
        # hosted canary (pydantic and yaml only) neither need nor install.
        from omnimarket.config.service_endpoints import service_endpoint_url

        token, channel = self._resolve()
        body = self._call(
            service_endpoint_url("slack.chat_post_message_url"),
            {"channel": channel, "text": text},
            token,
        )
        if body.get("ok") is not True:
            raise AlarmSendRefusedError(
                f"Slack refused the post: {str(body.get('error', 'no reason given'))[:60]}"
            )
        stamp = body.get("ts")
        if not isinstance(stamp, str) or not stamp:
            raise AlarmSendRefusedError(
                "Slack accepted the post and returned no timestamp"
            )
        return stamp

    def exists(self, message_ts: str) -> bool | None:
        from omnimarket.config.service_endpoints import service_endpoint_url

        try:
            token, channel = self._resolve()
            body = self._call(
                service_endpoint_url("slack.conversations_history_url"),
                {
                    "channel": channel,
                    "oldest": message_ts,
                    "latest": message_ts,
                    "inclusive": True,
                    "limit": 1,
                },
                token,
                form=True,
            )
        except AlarmSendRefusedError:
            return None
        if body.get("ok") is not True:
            return None
        messages = body.get("messages")
        if not isinstance(messages, list):
            return None
        return any(isinstance(m, dict) and m.get("ts") == message_ts for m in messages)


def build_slack_api(
    *,
    environ: Mapping[str, str],
    token_ref: str,
    channel_ref: str,
    env_file_env: str | None,
    timeout_s: int,
    http: Http = urllib_http,
) -> SlackWebApi:
    return SlackWebApi(
        credentials=OperatorEnvCredentials(environ, env_file_env),
        token_ref=token_ref,
        channel_ref=channel_ref,
        timeout_s=timeout_s,
        http=http,
    )


__all__ = [
    "OperatorEnvCredentials",
    "ProtocolCredentialSource",
    "ProtocolSlackApi",
    "SlackWebApi",
    "build_slack_api",
    "read_named_key",
]
