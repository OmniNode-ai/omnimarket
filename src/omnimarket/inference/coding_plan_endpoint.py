# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20173: keep Coding Plan quota out of direct GLM callers."""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


def addresses_coding_plan(url: str) -> bool:
    """Identify the Coding Plan path, independent of host, query or case."""
    path = urlsplit(url).path.casefold().rstrip("/")
    return "/api/coding/" in path or path.endswith("/api/coding")


def glm_url_or_empty(url: str, *, source: str) -> str:
    """Treat a Coding Plan endpoint as unset; log the reader, never credentials."""
    if addresses_coding_plan(url):
        logger.warning("%s: Coding Plan endpoint skipped (OMN-20173)", source)
        return ""
    return url
