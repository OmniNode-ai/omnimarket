# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain tests for the morning installer copy plan (OMN-24424).

The failing control: `plan_ignoring_mode` is the old behaviour (plan every artifact whatever
the mode) kept here as the named injected break; on a lib-only request it touches the bin
files, and the mode-scoped plan plans only the package lib.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.model_morning_trigger_install import (
    ModelMorningInstallArtifact,
    ModelMorningInstallPlan,
    ModelMorningInstallPolicy,
    ModelMorningInstallRequest,
)
from omnimarket.nodes.node_morning_trigger_install_compute.handlers import (
    handler_morning_trigger_install as handler_module,
)
from omnimarket.nodes.node_morning_trigger_install_compute.handlers.handler_morning_trigger_install import (
    HandlerMorningTriggerInstall,
)

OVERLAY = Path(__file__).resolve().parents[1] / "morning_install_policy.yaml"

ARTIFACTS = (
    ModelMorningInstallArtifact(
        name="driver",
        kind="bin",
        source="launchd/morning-workflows/morning_workflows_tick.sh",
        destination="$HOME/.omninode/morning-workflows/bin/morning_workflows_tick.sh",
    ),
    ModelMorningInstallArtifact(
        name="deferral_runner",
        kind="bin",
        source="launchd/morning-workflows/morning_deferral_refire.sh",
        destination="$HOME/.omninode/morning-workflows/bin/morning_deferral_refire.sh",
    ),
    ModelMorningInstallArtifact(
        name="deferral_template",
        kind="bin",
        source="launchd/morning-workflows/launchd/ai.omninode.morning-deferral.plist.tmpl",
        destination="$HOME/.omninode/morning-workflows/ai.omninode.morning-deferral.plist.tmpl",
    ),
    ModelMorningInstallArtifact(
        name="watchdog_driver",
        kind="bin",
        source="launchd/morning-workflows/morning_tick_watchdog.sh",
        destination="$HOME/.omninode/morning-workflows/bin/morning_tick_watchdog.sh",
    ),
    ModelMorningInstallArtifact(
        name="lib",
        kind="lib",
        source="lib/morning_workflows_package",
        destination="$HOME/.omninode/morning-workflows/lib",
    ),
)


def policy() -> ModelMorningInstallPolicy:
    """The packaged overlay, read with yaml.safe_load and validated as the policy."""
    raw = yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))
    return ModelMorningInstallPolicy.model_validate(raw)


def request(mode: str) -> ModelMorningInstallRequest:
    return ModelMorningInstallRequest(
        correlation_id="test",
        mode=mode,
        artifacts=ARTIFACTS,
        policy=policy(),
    )


def plan_ignoring_mode(request: ModelMorningInstallRequest) -> ModelMorningInstallPlan:
    """The old behaviour: plan every artifact whatever the mode says."""
    return ModelMorningInstallPlan(
        correlation_id=request.correlation_id,
        outcome="planned",
        mode=request.mode,
        copy_actions=request.artifacts,
        reload_labels=True,
    )


def test_failing_control_lib_only_old_behaviour_touches_bin_files() -> None:
    r = request("lib-only")
    old = plan_ignoring_mode(r)  # the break: ignores the mode and touches the bin files
    assert any(a.kind == "bin" for a in old.copy_actions)
    plan = HandlerMorningTriggerInstall().handle(r)
    assert [a.name for a in plan.copy_actions] == ["lib"]
    assert not any(a.kind == "bin" for a in plan.copy_actions)


def test_golden_lib_only_plans_only_the_lib_artifact_and_reloads_no_label() -> None:
    plan = HandlerMorningTriggerInstall().handle(request("lib-only"))
    assert plan.outcome == "planned"
    assert [a.name for a in plan.copy_actions] == ["lib"]
    assert plan.reload_labels is False


def test_golden_install_plans_all_five_and_reloads_the_labels() -> None:
    plan = HandlerMorningTriggerInstall().handle(request("install"))
    assert plan.outcome == "planned"
    assert [a.name for a in plan.copy_actions] == [
        "driver",
        "deferral_runner",
        "deferral_template",
        "watchdog_driver",
        "lib",
    ]
    assert plan.reload_labels is True


def test_error_unknown_mode_is_refused_with_no_copy_actions() -> None:
    plan = HandlerMorningTriggerInstall().handle(request("bogus"))
    assert plan.outcome == "refused"
    assert plan.refusal_code == "unknown_mode"
    assert plan.copy_actions == ()
    assert "bogus" in plan.detail
    assert "install" in plan.detail
    assert "lib-only" in plan.detail


def test_error_artifact_kind_must_be_bin_or_lib() -> None:
    with pytest.raises(ValidationError):
        ModelMorningInstallArtifact(name="x", kind="other", source="s", destination="d")


def test_seam_packaged_overlay_names_exactly_the_two_modes() -> None:
    assert set(policy().modes) == {"install", "lib-only"}


def test_the_handler_reads_no_file_and_carries_no_purity_suppression() -> None:
    source = Path(handler_module.__file__ or "").read_text(encoding="utf-8")
    assert "import yaml" not in source
    assert "read_text" not in source
    assert "node-purity-ok" not in source
