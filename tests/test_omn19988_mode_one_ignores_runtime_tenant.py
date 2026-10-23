# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19988: mode 1 (`onex dashboard`, minted identity) is unchanged.

The native report is written under the identity the dashboard was started with.
The runtime's configured tenant is the compose observer's fallback and must not
reach it: a laptop whose shell carries a stray ``ONEX_TENANT_ID`` would
otherwise report its native row under another tenant than the one it serves.

Failure mode: the native row is stored under the configured tenant, or not
stored at all, because the observer's fallback leaked into the shared tenant
resolution.
"""

from __future__ import annotations

import pytest

from omnimarket.config.settings import get_settings
from tests.test_omn19988_native_environment_report import (
    TENANT,
    _rows,
    _start,
)

pytestmark = pytest.mark.unit


def test_a_configured_runtime_tenant_does_not_move_the_native_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("ONEX_TENANT_ID", "local-7eac03d77ba9")
    get_settings.cache_clear()
    try:
        db_path = tmp_path / "delegation.sqlite"
        _start(monkeypatch, db_path, tmp_path)
        assert [(r["tenant_id"], r["mode"]) for r in _rows(db_path)] == [
            (TENANT, "native")
        ]
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
