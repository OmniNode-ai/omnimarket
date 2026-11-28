# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The broker lane comes from the environment variable the contract names; the counts come from the contract."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_tenant_topic_provision_effect.broker_settings import (
    load_settings,
)

pytestmark = pytest.mark.unit

_LANE_ENV = "ONEX_TENANT_TOPIC_PROVISION_LANE"


def test_settings_take_the_lane_from_the_named_variable_and_the_counts_from_the_contract() -> (
    None
):
    settings = load_settings({_LANE_ENV: "synthetic-lane"})
    assert settings.lane == "synthetic-lane"
    assert settings.num_partitions >= 1
    assert settings.replication_factor >= 1


def test_settings_refuse_to_guess_a_lane() -> None:
    with pytest.raises(ValueError, match=_LANE_ENV):
        load_settings({})


def test_the_contract_declares_no_overlay() -> None:
    from importlib import resources

    import yaml

    text = (
        resources.files("omnimarket.nodes.node_tenant_topic_provision_effect")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    assert "overlay" not in yaml.safe_load(text)
