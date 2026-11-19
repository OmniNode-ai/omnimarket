# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden, error and contract tests for the plugin repair attempt judgement (OMN-24194).

The failing control: `exit_code_only_judgement` is the old behaviour (an install that exits 0 is applied) kept here as the
named injected break; it reads the h101 no-op install of 2026-11-13 as applied, and the judgement reads it as alert only.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import get_type_hints

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.model_plugin_install_judgement import (
    EnumPluginInstallJudgement,
    ModelPluginInstallObservation,
)
from omnimarket.nodes.node_host_drift_plugin_install_judge_compute.handlers.handler_plugin_install_judge import (
    HandlerHostDriftPluginInstallJudge,
)

OMNI = "omni@omninode-internal"
COMMIT, NEXT = "5b16f7640b2e" + "0" * 28, "c" * 40
PACKAGE = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_host_drift_plugin_install_judge_compute"
)


def observation(**over: object) -> ModelPluginInstallObservation:
    base: dict[str, object] = {
        "name": OMNI,
        "action": "install-plugin-tree",
        "exit_code": 0,
        "before": {"revision": COMMIT, "version": "0.4.475"},
        "after": {"revision": COMMIT, "version": "0.4.475"},
    }
    base.update(over)
    return ModelPluginInstallObservation.model_validate(base)


def exit_code_only_judgement(o: ModelPluginInstallObservation) -> str:
    """The old judgement: an install that exits 0 is applied, whatever the record says."""
    return "APPLIED" if o.exit_code == 0 else "FAILED"


def test_failing_control_no_op_install_is_not_applied() -> None:
    o = observation()
    assert (
        exit_code_only_judgement(o) == "APPLIED"
    )  # the break: three identical installs
    judgement = HandlerHostDriftPluginInstallJudge().handle(o)
    assert judgement.outcome == EnumPluginInstallJudgement.ALERT_ONLY
    assert judgement.counts_as_attempt is False
    assert judgement.retry_allowed is False
    assert judgement.detail == (
        "install-plugin-tree left the installed record unchanged at "
        "revision 5b16f7640b2e version 0.4.475: the same install again would change nothing"
    )


def test_golden_install_that_moved_the_revision_is_applied() -> None:
    judgement = HandlerHostDriftPluginInstallJudge().handle(
        observation(after={"revision": NEXT, "version": "0.4.475"})
    )
    assert judgement.outcome == EnumPluginInstallJudgement.APPLIED
    assert judgement.counts_as_attempt is True
    assert judgement.retry_allowed is True
    assert judgement.detail == (
        "install-plugin-tree moved the installed record from revision 5b16f7640b2e version 0.4.475 "
        f"to revision {NEXT[:12]} version 0.4.475"
    )


def test_golden_install_that_moved_only_the_version_is_applied() -> None:
    judgement = HandlerHostDriftPluginInstallJudge().handle(
        observation(
            action="update-plugin", after={"revision": COMMIT, "version": "0.4.476"}
        )
    )
    assert judgement.outcome == EnumPluginInstallJudgement.APPLIED
    assert judgement.counts_as_attempt is True


def test_golden_failed_install_spends_an_attempt_and_may_retry() -> None:
    judgement = HandlerHostDriftPluginInstallJudge().handle(
        observation(exit_code=1, after=None)
    )
    assert judgement.outcome == EnumPluginInstallJudgement.FAILED
    assert judgement.counts_as_attempt is True
    assert judgement.retry_allowed is True
    assert judgement.detail == "install-plugin-tree exited 1"


def test_error_unreadable_record_after_a_clean_exit_is_alert_only() -> None:
    judgement = HandlerHostDriftPluginInstallJudge().handle(
        observation(after=None, before={"revision": None, "version": None})
    )
    assert judgement.outcome == EnumPluginInstallJudgement.ALERT_ONLY
    assert judgement.retry_allowed is False
    assert judgement.detail == (
        "install-plugin-tree exited 0 but the installed record could not be read after it; "
        "it held revision unread version unread before"
    )


def test_error_unknown_field_is_refused() -> None:
    with pytest.raises(ValidationError):
        observation(surprise=True)


def test_contract_bindings() -> None:
    contract = yaml.safe_load((PACKAGE / "contract.yaml").read_text())
    assert contract["name"] == "node_host_drift_plugin_install_judge_compute"
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    command_topic = "onex.cmd.omnimarket.host-drift-plugin-install-judge-requested.v1"
    assert command_topic in contract["event_bus"]["subscribe_topics"]
    assert command_topic == contract["runtime_dispatch"]["command_topic"]
    terminal_topic = "onex.evt.omnimarket.host-drift-plugin-install-judged.v1"
    assert terminal_topic == contract["terminal_event"]
    assert terminal_topic in contract["event_bus"]["publish_topics"]
    assert terminal_topic in [p["topic"] for p in contract["published_events"]]
    binding = contract["handler"]
    handler = getattr(importlib.import_module(binding["module"]), binding["class"])
    hints = get_type_hints(handler.handle)
    for key, hint in (("input_model", "request"), ("output_model", "return")):
        module, _, name = binding[key].rpartition(".")
        model = getattr(importlib.import_module(module), name)
        assert hints[hint] is model
        declaration = contract[key]
        assert (
            getattr(importlib.import_module(declaration["module"]), declaration["name"])
            is model
        )
