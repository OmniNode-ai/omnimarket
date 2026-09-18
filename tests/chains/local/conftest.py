# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixtures shared by every local-path chain pair (OMN-18698)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from omnimarket.routing import byok_provider_backends
from tests.chains.local.harness import provider_stub

__all__ = ["provider_stub"]


@pytest.fixture(autouse=True)
def _restore_byok_catalogue_cache() -> Iterator[None]:
    """Drop the process-lifetime catalogue cache around every pair.

    ``load_byok_provider_catalog`` is ``lru_cache``d because the shipped file
    cannot change under a running consumer. A pair that repoints
    ``CATALOG_PATH`` therefore has to clear it on the way in AND on the way
    out -- without the second clear the rig's catalogue would leak into every
    later test in the session, which is a far worse failure than a red pair
    because it would show up somewhere else.
    """
    byok_provider_backends.load_byok_provider_catalog.cache_clear()
    byok_provider_backends.load_byok_not_offered_providers.cache_clear()
    yield
    byok_provider_backends.load_byok_provider_catalog.cache_clear()
    byok_provider_backends.load_byok_not_offered_providers.cache_clear()
