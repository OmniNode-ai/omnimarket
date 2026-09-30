# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI test fixtures."""

from __future__ import annotations

import pytest

from omnimarket.routing.byok_plan_detection import ModelByokPlanDetection


@pytest.fixture(autouse=True)
def _no_network_plan_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """OMN-20157: ``onex secret set`` detects a multi-plan provider's plan.

    ``glm`` has two plans, so storing a glm key sends it to z.ai to find out
    which. A CLI test that reached the real endpoint would send its placeholder
    key to a third party, so detection is stubbed for every test in this
    directory. ``test_omn20157_secret_set_plan.py`` overrides the stub to pin
    the detection behaviour itself, over a mock transport.
    """

    async def _general(
        provider: str, api_key: object, **_: object
    ) -> ModelByokPlanDetection:
        return ModelByokPlanDetection(
            provider=provider, plan="general_api", outcome="detected"
        )

    monkeypatch.setattr("omnimarket.cli.cli_secret.detect_byok_plan", _general)
