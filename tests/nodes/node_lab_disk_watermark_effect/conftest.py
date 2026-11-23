# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The watermark tests read the byte share from an injected usage; the inode share is injected the same way.

A test that does not inject ``inodes`` must not read the real filesystem of whichever host runs it (a lab host short of
inodes would turn every unrelated watermark test red), so the default inode reader reads a healthy filesystem here.
Tests of the inode alarm inject their own readings and are not affected.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_lab_disk_watermark_effect.handlers import (
    handler_lab_disk_watermark,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.handlers.inode_alarm import (
    InodeUsage,
)


@pytest.fixture(autouse=True)
def _healthy_inodes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        handler_lab_disk_watermark,
        "inode_usage",
        lambda _path: InodeUsage(1_000_000, 500_000),
    )
