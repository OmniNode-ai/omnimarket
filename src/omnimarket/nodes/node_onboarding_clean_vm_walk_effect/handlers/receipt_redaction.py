# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One sanitiser over every string a published walk receipt carries.

The receipt is published on the bus and printed to stdout, so a credential the
onboarding script prints, or one embedded in a URL the request names, would reach
every reader. ``sanitize_receipt`` rebuilds the finished receipt with every
string field passed through ``redact``; ``secret_findings`` is the detection
half, reporting only the pattern class, offset and length of each match.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from pydantic import BaseModel

PLACEHOLDER = "REDACTED"

_ASSIGNMENT_KEYS = (
    r"password|passwd|secret|client[_-]?secret|api[_-]?key|apikey|"
    r"access[_-]?key|private[_-]?key|auth[_-]?token|access[_-]?token|token"
)


class _Rule:
    """A pattern whose ``secret`` group is the value to remove."""

    def __init__(
        self,
        name: str,
        pattern: str,
        flags: int = 0,
        replace: Callable[[re.Match[str]], str] | None = None,
    ) -> None:
        self.name = name
        self.pattern = re.compile(pattern, flags)
        self._replace = replace

    @staticmethod
    def _value(match: re.Match[str]) -> str:
        return match.group("secret").strip("\"'")

    def is_live(self, match: re.Match[str]) -> bool:
        value = self._value(match)
        return bool(value) and value != PLACEHOLDER

    def substitute(self, match: re.Match[str]) -> str:
        if not self.is_live(match):
            return match.group(0)
        if self._replace is not None:
            return self._replace(match)
        start, end = match.span("secret")
        whole_start = match.start()
        text = match.group(0)
        secret = match.group("secret")
        quote = (
            secret[0] if secret[:1] in {'"', "'"} and secret[-1:] == secret[0] else ""
        )
        return (
            text[: start - whole_start]
            + quote
            + PLACEHOLDER
            + quote
            + text[end - whole_start :]
        )


def _drop_userinfo(match: re.Match[str]) -> str:
    return match.group("scheme")


def _drop_ssh_password(match: re.Match[str]) -> str:
    return match.group("user") + "@"


# Order matters: multi-line blocks first, then URL parts, then free-text shapes.
_RULES: tuple[_Rule, ...] = (
    _Rule(
        "pem_private_key",
        r"(?P<secret>-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"
        r"[\s\S]*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\Z))",
    ),
    _Rule(
        # Any scheme: scheme://user:secret@host and scheme://token@host -> scheme://host.
        "url_userinfo",
        r"(?P<scheme>\b[A-Za-z][A-Za-z0-9+.-]*://)(?P<secret>[^\s/?#\"'<>]*)@",
        replace=_drop_userinfo,
    ),
    _Rule(
        "url_query",
        r"(?P<url>\b[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>?]+)\?(?P<secret>[^\s\"'<>]*)",
    ),
    _Rule(
        # SSH style user:secret@host keeps user@host; plain user@host has no secret.
        "ssh_userinfo_password",
        r"(?<![\w/:.@%+~-])(?P<user>[A-Za-z0-9._~%+-]+):(?P<secret>[^\s@/:\"'<>]+)@"
        r"(?=\[|[A-Za-z0-9])",
        replace=_drop_ssh_password,
    ),
    _Rule(
        "authorization_header",
        r"(?P<key>\bauthorization[\"']?\s*[:=]\s*[\"']?"
        r"(?:(?:bearer|basic|token|digest|bot)\s+)?)(?P<secret>[^\s\"',;]+)",
        re.IGNORECASE,
    ),
    _Rule(
        "bearer_token",
        r"\bbearer\s+(?P<secret>[A-Za-z0-9._~+/=-]{8,})",
        re.IGNORECASE,
    ),
    _Rule(
        "secret_assignment",
        r"\b(?:[A-Za-z0-9]+[_-])*(?:" + _ASSIGNMENT_KEYS + r")(?:[_-][A-Za-z0-9]+)*"
        r"[\"']?\s*[=:]\s*(?P<secret>\"[^\"\n]*\"|'[^'\n]*'|[^\s&\"';,<>]+)",
        re.IGNORECASE,
    ),
    _Rule("openai_style_key", r"(?P<secret>\bsk-[A-Za-z0-9_-]{16,})"),
    _Rule(
        "github_token",
        r"(?P<secret>\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}))",
    ),
    _Rule("slack_token", r"(?P<secret>\bxox[abprs]-[A-Za-z0-9-]{10,})"),
    _Rule("aws_access_key_id", r"(?P<secret>\b(?:AKIA|ASIA)[0-9A-Z]{16}\b)"),
    _Rule("gitlab_token", r"(?P<secret>\bglpat-[A-Za-z0-9_-]{20,})"),
    _Rule("google_api_key", r"(?P<secret>\bAIza[0-9A-Za-z_-]{30,})"),
    _Rule("huggingface_token", r"(?P<secret>\bhf_[A-Za-z0-9]{30,})"),
    _Rule("linear_api_key", r"(?P<secret>\blin_api_[A-Za-z0-9]{30,})"),
    _Rule(
        "jwt",
        r"(?P<secret>\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})",
    ),
)


def redact(text: str) -> str:
    """Remove URL user information, queries and every secret-shaped value."""
    for rule in _RULES:
        text = rule.pattern.sub(rule.substitute, text)
    return text


def secret_findings(text: str) -> tuple[tuple[str, int, int], ...]:
    """(pattern class, offset, value length) per secret-shaped match; never the value."""
    found: list[tuple[str, int, int]] = []
    for rule in _RULES:
        for match in rule.pattern.finditer(text):
            if rule.is_live(match):
                start, end = match.span("secret")
                found.append((rule.name, start, end - start))
    return tuple(sorted(found, key=lambda item: item[1]))


def _redact_value(value: object) -> object:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_value(item) for item in value]
    return value


def sanitize_receipt[ModelT: BaseModel](receipt: ModelT) -> ModelT:
    """Rebuild a finished receipt with every string field, at any depth, redacted."""
    dumped = _redact_value(receipt.model_dump(mode="json"))
    return type(receipt).model_validate(dumped)
