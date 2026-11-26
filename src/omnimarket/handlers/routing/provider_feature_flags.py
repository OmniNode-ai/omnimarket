# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Providers a feature flag keeps off until it is set (OMN-26688).

The flags live in ``provider_feature_flags.yaml`` beside this file, one line per
provider. While a provider's flag is unset it is left out of the provider list,
the Config tab's choices and the backend catalog (so no route is made for a key
saved earlier), ``onex models list`` does not show it, and a new key for it is
refused. A key already saved stays where it is.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

import yaml

FLAGS_PATH = Path(__file__).with_name("provider_feature_flags.yaml")
FLAGS_SCHEMA = "provider_feature_flags.v1"
_FLAG_NAME = re.compile(r"^ONEX_ENABLE_[A-Z0-9_]+$")
_ON = frozenset({"1", "true", "yes", "on"})


class ProviderFlagsError(ValueError):
    """The flags file is malformed."""


@lru_cache(maxsize=1)
def provider_flags() -> dict[str, str]:
    """Each flagged provider id and the environment variable that turns it on."""
    document = yaml.safe_load(FLAGS_PATH.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != FLAGS_SCHEMA:
        raise ProviderFlagsError(f"{FLAGS_PATH.name} is not {FLAGS_SCHEMA}")
    flags = document.get("flags")
    if not isinstance(flags, dict):
        raise ProviderFlagsError(f"{FLAGS_PATH.name} has no 'flags' mapping")
    for provider, flag in flags.items():
        if not isinstance(provider, str) or not isinstance(flag, str):
            raise ProviderFlagsError(f"{FLAGS_PATH.name}: {provider!r} is not text")
        if not _FLAG_NAME.match(flag):
            raise ProviderFlagsError(
                f"{FLAGS_PATH.name}: {flag!r} is not ONEX_ENABLE_*"
            )
    return dict(flags)


def provider_turned_off(provider: str) -> str | None:
    """The flag keeping ``provider`` off while it is unset, else ``None``.

    Read from the environment at each call; the catalogs that use it are cached
    for the process, so a flag set takes effect the next time onex starts.
    """
    flag = provider_flags().get(provider.strip().lower())
    if flag is None:
        return None
    return None if os.environ.get(flag, "").strip().lower() in _ON else flag
