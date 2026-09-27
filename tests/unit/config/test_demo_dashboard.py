# SPDX-License-Identifier: MIT
"""Contract-declared demo dashboard endpoint binding."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.config.demo_dashboard import ModelDemoDashboardEndpoint


@pytest.mark.unit
def test_absent_dashboard_url_is_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEMO_DASHBOARD_URL", raising=False)
    assert ModelDemoDashboardEndpoint.from_environment().base_url is None


@pytest.mark.unit
def test_valid_dashboard_url_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_DASHBOARD_URL", " https://dashboard.example.test/ ")
    assert (
        ModelDemoDashboardEndpoint.from_environment().base_url
        == "https://dashboard.example.test"
    )


@pytest.mark.unit
def test_invalid_dashboard_url_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_DASHBOARD_URL", "localhost:3000")
    with pytest.raises(ValidationError):
        ModelDemoDashboardEndpoint.from_environment()
