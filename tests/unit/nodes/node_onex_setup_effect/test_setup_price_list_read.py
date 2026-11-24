# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex setup`` is where the model price list is read, so a session never reads it."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers import model_catalog
from omnimarket.nodes.node_onex_setup_effect.handlers import (
    handler_setup_environment as environment,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

pytestmark = pytest.mark.unit


def _plan(tmp_path: Path) -> ModelSetupPlan:
    return ModelSetupPlan(
        workspace=tmp_path,
        providers=[{"provider": "openai", "models": ["selected-model"]}],
    )


def test_applying_the_models_step_reads_the_price_list_into_the_onex_home_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    monkeypatch.setattr(
        model_catalog,
        "refresh_models_dev",
        lambda onex_home=None: (
            order.append(f"refresh {onex_home}") or model_catalog.EMPTY_INDEX
        ),
    )
    monkeypatch.setattr(
        environment.HandlerSetupEnvironment,
        "_apply_provider",
        lambda _self, _plan, desired: order.append(f"provider {desired.provider}"),
    )
    handler = environment.HandlerSetupEnvironment(name="models", onex_home=tmp_path)
    assert handler.apply(_plan(tmp_path)) is None
    assert order == [f"refresh {tmp_path}", "provider openai"]


def test_a_failed_price_list_read_does_not_fail_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreachable(onex_home: Path | None = None) -> None:
        raise OSError("models.dev unreachable in this test")

    applied: list[str] = []
    monkeypatch.setattr(model_catalog, "refresh_models_dev", unreachable)
    monkeypatch.setattr(
        environment.HandlerSetupEnvironment,
        "_apply_provider",
        lambda _self, _plan, desired: applied.append(desired.provider),
    )
    handler = environment.HandlerSetupEnvironment(name="models", onex_home=tmp_path)
    assert handler.apply(_plan(tmp_path)) is None
    assert applied == ["openai"]
