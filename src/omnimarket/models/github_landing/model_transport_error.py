# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed transport/auth failure for GitHub API calls (OMN-23854).

Held here so callers can distinguish a transport failure from an empty
result (repo has no open PRs) without importing the node's protocol module.

A rate-limit answer is its own typed refusal, :class:`GitHubRateLimitedError`
(OMN-26326): it says the credential's quota is spent and when GitHub says it
resets, instead of folding into a bare ``HTTP 403`` / ``HTTP 429`` message.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final

GITHUB_RATE_LIMITED: Final[str] = "GITHUB-RATE-LIMITED"

_RATE_LIMIT_STATUSES: Final[frozenset[int]] = frozenset({403, 429})
_MAX_MESSAGE_CHARS: Final[int] = 300


class GitHubTransportError(Exception):
    """Raised when a GitHub API call fails due to a transport or auth error.

    Distinct from an empty result (repo has no open PRs), so callers can
    abort rather than silently treating an outage as an empty list.
    """


class GitHubRateLimitedError(GitHubTransportError):
    """GitHub refused a request because the credential's rate limit is spent.

    A subclass of :class:`GitHubTransportError`, so every existing handler of
    the transport error still catches it. ``reset_epoch`` is GitHub's
    ``x-ratelimit-reset`` (epoch seconds) and ``retry_after_seconds`` its
    ``retry-after``; each is ``None`` when GitHub did not send it, never a
    guess.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int,
        reset_epoch: int | None,
        retry_after_seconds: int | None,
        resource: str | None,
        limit: int | None,
        remaining: int | None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.reset_epoch = reset_epoch
        self.retry_after_seconds = retry_after_seconds
        self.resource = resource
        self.limit = limit
        self.remaining = remaining


def _whole_number(value: str | None) -> int | None:
    if value is None or not value.strip().isdigit():
        return None
    return int(value.strip())


def rate_limit_refusal(
    *,
    status: int,
    headers: Mapping[str, str],
    message: str,
    request_line: str,
) -> GitHubRateLimitedError | None:
    """The typed refusal for a rate-limit answer, or ``None`` for any other answer.

    GitHub signals a rate limit as HTTP 429, or as HTTP 403 with
    ``x-ratelimit-remaining: 0`` (primary limit), a ``retry-after`` header or a
    message naming the rate limit (secondary limit). A 403 with quota left and
    no such message is a permission refusal and stays a plain transport error.
    """
    if status not in _RATE_LIMIT_STATUSES:
        return None
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    remaining = _whole_number(lowered.get("x-ratelimit-remaining"))
    retry_after = _whole_number(lowered.get("retry-after"))
    if not (
        status == 429
        or remaining == 0
        or retry_after is not None
        or "rate limit" in message.lower()
    ):
        return None
    reset_epoch = _whole_number(lowered.get("x-ratelimit-reset"))
    resource = lowered.get("x-ratelimit-resource")
    if reset_epoch is not None:
        when = (
            f"resets at {datetime.fromtimestamp(reset_epoch, UTC).isoformat()} "
            f"(epoch {reset_epoch})"
        )
    elif retry_after is not None:
        when = f"retry after {retry_after} s"
    else:
        when = "reset time not reported by GitHub"
    return GitHubRateLimitedError(
        f"{GITHUB_RATE_LIMITED}: {request_line} answered HTTP {status}: "
        f"{message[:_MAX_MESSAGE_CHARS]}; quota resource="
        f"{resource or 'unreported'} remaining="
        f"{remaining if remaining is not None else 'unreported'}; {when}",
        status=status,
        reset_epoch=reset_epoch,
        retry_after_seconds=retry_after,
        resource=resource,
        limit=_whole_number(lowered.get("x-ratelimit-limit")),
        remaining=remaining,
    )


__all__ = [
    "GITHUB_RATE_LIMITED",
    "GitHubRateLimitedError",
    "GitHubTransportError",
    "rate_limit_refusal",
]
