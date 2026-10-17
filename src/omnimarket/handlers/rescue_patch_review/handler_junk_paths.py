# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recognize regenerable output while preserving secret-shaped paths."""

import fnmatch
import re

_JUNK_ANYWHERE = re.compile(
    r"(^|/)(\.venv|venv|__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|"
    r"node_modules|htmlcov|\.hypothesis|\.tox|\.nox|\.uv-cache|\.cache|"
    r"[^/]+\.egg-info|\.coverage[^/]*|\.DS_Store)(/|$)|\.py[co]$"
)

_JUNK_TOP = re.compile(r"^(dist|build)(/|$)")

_SECRET_SHAPED = (
    ".env",
    ".env.*",
    "*.env",
    ".envrc",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".pgpass",
    "credentials",
    "credentials.json",
    "*.tfvars",
    "settings.local.json",
)


def is_secret_shaped(path: str) -> bool:
    name = path.rstrip("/").rsplit("/", 1)[-1].lower()
    return any(fnmatch.fnmatchcase(name, glob) for glob in _SECRET_SHAPED)


def is_junk(path: str) -> bool:
    if is_secret_shaped(path):
        return False
    return bool(_JUNK_ANYWHERE.search(path) or _JUNK_TOP.search(path))
