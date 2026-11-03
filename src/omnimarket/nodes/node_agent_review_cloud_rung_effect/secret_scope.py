# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What never reaches a cloud session: the signing key and every secret-shaped name of the environment."""

from __future__ import annotations

import re
from collections.abc import Mapping

SIGNING_SECRET_NAME = "LOCAL_LLM_SHARED_SECRET"
"""The dispatch node's signing key. It never reaches a brief, a session or a session's environment."""

SECRET_NAME_RE = re.compile(
    r"(SECRET|TOKEN|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL|PRIVATE|(^|_)KEY($|_)|API_?KEY|SIGNING)",
    re.IGNORECASE,
)
_SECRET_VALUE_MIN = 6


def is_secret_name(name: str) -> bool:
    return name == SIGNING_SECRET_NAME or bool(SECRET_NAME_RE.search(name))


def runner_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The environment the cloud lane runner starts with: every secret-shaped name dropped."""
    return {k: v for k, v in environ.items() if not is_secret_name(k)}


def secret_leaks(text: str, environ: Mapping[str, str]) -> list[str]:
    """The secret names (never values) whose name or value the text contains."""
    leaks = [SIGNING_SECRET_NAME] if SIGNING_SECRET_NAME in text else []
    for name, value in environ.items():
        if not is_secret_name(name) or name in leaks:
            continue
        if name in text or (len(value) >= _SECRET_VALUE_MIN and value in text):
            leaks.append(name)
    return leaks


__all__ = [
    "SECRET_NAME_RE",
    "SIGNING_SECRET_NAME",
    "is_secret_name",
    "runner_environment",
    "secret_leaks",
]
