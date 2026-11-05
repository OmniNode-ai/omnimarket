# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Registered daily node: frozen facts -> labels -> committed evidence -> demotion."""

import tomllib
from importlib import import_module
from pathlib import Path

import pytest

from omnimarket.models.error_rule_measurement import (
    ModelDailyMeasurementSnapshot,
    ModelMeasurementTick,
)
from omnimarket.nodes.node_error_rule_measurement_effect import (
    NodeErrorRuleMeasurementEffect,
)
from omnimarket.nodes.node_error_rule_measurement_effect.handlers.handler_daily import (
    HandlerErrorRuleMeasurementDaily,
)
from tests.unit.nodes.node_error_rule_measurement_effect.test_promotion_measurement import (
    NOW,
    live_lab,
)


def registered_node(lab):
    root = Path(__file__).parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text())
    module_name = metadata["project"]["entry-points"]["onex.nodes"][
        "node_error_rule_measurement_effect"
    ]
    assert (
        import_module(module_name).NodeErrorRuleMeasurementEffect
        is NodeErrorRuleMeasurementEffect
    )
    command = lab.command()

    class Source:
        def snapshot(self, now):
            assert now == NOW
            return ModelDailyMeasurementSnapshot(
                row_digests=(command.row_digest,),
                corpus_digest=command.corpus_digest,
                normalizer_digest=command.normalizer_digest,
                live_digest=command.live_digest,
            )

    return NodeErrorRuleMeasurementEffect(
        HandlerErrorRuleMeasurementDaily(
            overlay=lab.handler().overlay,
            artifacts=lab,
            replay=lab.replay,
            labeller=lab.labels,
            writer=lab,
            source=Source(),
        )
    )


def test_golden_chain_promotion_measurement_commits_then_proposes_demotion():
    lab = live_lab()
    node = registered_node(lab)
    first = node.handle(ModelMeasurementTick(now=NOW))
    second = node.handle(ModelMeasurementTick(now=NOW))
    assert first == second
    assert len(first.measurements) == 1
    result = first.measurements[0]
    assert result.read_back
    assert result.measured.true_matches == 8
    assert result.measured.prospective.restatement_ids == ("capture-1",)
    assert len(lab.measurements) == 1
    assert len(lab.demotions) == 1
    demotion = next(iter(lab.demotions.values()))
    assert demotion.target_mode == "shadow"
    assert demotion.capture_ids == ("capture-1",)
    assert demotion.measured == result.measured


def test_golden_chain_promotion_measurement_error_leg_refuses_foreign_writer_before_demotion():
    lab = live_lab()
    lab.login = "another-login"
    node = registered_node(lab)
    with pytest.raises(ValueError, match="identity"):
        node.handle(ModelMeasurementTick(now=NOW))
    assert len(lab.measurements) == 1
    assert lab.demotions == {}
