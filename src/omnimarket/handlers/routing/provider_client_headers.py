# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Headers a provider's own docs ask every client to send (OMN-26688).

OpenCode (opencode.ai/docs/go, "Where can I use it?", read 2026-11-26): a client
should "Identify itself with its own user agent, such as my-coding-agent/1.0,
rather than a generic SDK or HTTP-library name" and "Send a stable session ID in
x-opencode-session for each conversation"; the Console harnesses page writes it
``x-opencode-session: ses_...`` and says Go requires it. One delegation run is
one conversation, so its correlation id is the session: the same across the
run's retries, new for the next run.
"""

from __future__ import annotations

import re
from functools import cache
from importlib import metadata
from urllib.parse import urlparse

#: The provider's host -> the header its docs name for the conversation.
SESSION_HEADERS: dict[str, str] = {"opencode.ai": "x-opencode-session"}
_SESSION_PREFIX = "ses_"
_NOT_ID = re.compile(r"[^A-Za-z0-9]")


@cache
def onex_user_agent() -> str:
    """onex's own name and version, never a generic HTTP-library name."""
    try:
        return f"onex/{metadata.version('omnimarket')}"
    except metadata.PackageNotFoundError:
        return "onex"


def client_headers(url: str | None, conversation_id: str | None) -> dict[str, str]:
    """What the provider at ``url`` asks a client to send; nothing for any other.

    Without a conversation (a model list read) only the user agent is sent.
    """
    host = (urlparse(url or "").hostname or "").lower()
    header = SESSION_HEADERS.get(host)
    if header is None:
        return {}
    headers = {"User-Agent": onex_user_agent()}
    session = _NOT_ID.sub("", conversation_id or "")
    if session:
        headers[header] = _SESSION_PREFIX + session
    return headers
