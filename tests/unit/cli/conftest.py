# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_core.models.event_bus.model_run_node_hosted import ModelRunNodeHosted

from omnimarket.handlers.routing.byok_plan_detection import ModelByokPlanDetection


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

    monkeypatch.setattr(
        "omnimarket.handlers.handler_model_keys.detect_byok_plan", _general
    )


@pytest.fixture(autouse=True)
def _not_signed_in_to_a_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """OMN-26731: a test is not signed in unless it says so.

    ``register-tenant-key`` goes to the gateway when this machine holds a stored
    sign-in; a lab host that does must not turn the bus-path tests into gateway
    calls. ``test_omn26731_register_tenant_key_gateway.py`` overrides the stub.
    """
    monkeypatch.setattr("omnimarket.cli.cli_secret._signed_in_gateway", lambda: None)


def _delivery_not_required(node_id: str, state_root: Path) -> None:
    """Stand in for core's delivery check: these tests read the walk's output, not the bus."""
    del node_id, state_root


@pytest.fixture(autouse=True)
def _hosted_run_without_a_hosted_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let ``onex setup`` end without a hosted bus to carry the run's recorded events (OMN-26115).

    ``ModelRunNodeHosted.run`` refuses a finished run whose recorded events no forwarder
    delivered. The setup tests here drive the whole walk on a throwaway HOME and read its
    output; no hosted bus or forwarder overlay exists in the test environment, so only that
    delivery check is replaced and the run itself stays real.
    """
    monkeypatch.setattr(
        ModelRunNodeHosted, "require_delivered", staticmethod(_delivery_not_required)
    )
