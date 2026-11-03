# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two page sources the publish ships, the Dashboard and the Floor, carry the Memory used label."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    NODE_DIR,
)

pytestmark = pytest.mark.unit

PAGE_SOURCES = ("page/index.html", "page/floor/index.html")


@pytest.mark.parametrize("relative", PAGE_SOURCES)
def test_page_source_carries_memory_used_label(relative: str) -> None:
    source = (NODE_DIR / relative).read_text(encoding="utf-8")
    assert 'gauge("Memory used"' in source
