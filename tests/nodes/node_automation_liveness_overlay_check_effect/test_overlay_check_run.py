# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay completeness check run reads its inputs and reports what the command line prints (OMN-20804)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_automation_liveness_overlay_check_effect.handlers import (
    HandlerAutomationLivenessOverlayCheckRun,
)
from omnimarket.nodes.node_automation_liveness_overlay_check_effect.models import (
    ModelAutomationLivenessOverlayCheckRunRequest,
)
from tests.nodes.node_automation_liveness_overlay_check_compute.support import (
    entry_yaml,
    overlay_yaml,
    sources_yaml,
    write,
)

pytestmark = pytest.mark.unit

_OVERLAYS = "src/private_repo/overlays"
_NODE = Path(__file__).resolve().parents[3] / (
    "src/omnimarket/nodes/node_automation_liveness_overlay_check_effect"
)
_COMMAND_TOPIC = (
    "onex.cmd.omnimarket.automation-liveness-overlay-check-run-requested.v1"
)
_COMPLETED_TOPIC = (
    "onex.evt.omnimarket.automation-liveness-overlay-check-run-completed.v1"
)


def _private_checkout(tmp_path: Path) -> Path:
    internal = tmp_path / "private_repo"
    write(
        internal,
        f"{_OVERLAYS}/automation_liveness_overlay.yaml",
        overlay_yaml(entry_yaml()),
    )
    write(
        internal,
        f"{_OVERLAYS}/automation_liveness_sources.yaml",
        sources_yaml(repositories=("private_repo", "omnibase_infra")),
    )
    return internal


def test_overlay_check_run_exits_2_when_the_overlay_is_missing(tmp_path: Path) -> None:
    result = HandlerAutomationLivenessOverlayCheckRun().handle(
        ModelAutomationLivenessOverlayCheckRunRequest(
            internal_root=tmp_path, internal_only=True
        )
    )

    assert result.exit_code == 2
    assert "automation_liveness_overlay.yaml is missing" in result.stderr_lines[0]
    assert result.stdout_lines == ()


def test_overlay_check_run_exits_2_without_a_registry_root_unless_internal_only(
    tmp_path: Path,
) -> None:
    internal = _private_checkout(tmp_path)

    refused = HandlerAutomationLivenessOverlayCheckRun().handle(
        ModelAutomationLivenessOverlayCheckRunRequest(internal_root=internal)
    )
    partial = HandlerAutomationLivenessOverlayCheckRun().handle(
        ModelAutomationLivenessOverlayCheckRunRequest(
            internal_root=internal, internal_only=True
        )
    )

    assert refused.exit_code == 2
    assert "set OMNI_HOME or pass --registry-root" in refused.stderr_lines[0]
    assert partial.stderr_lines == ()
    assert "scope PARTIAL (internal only)" in partial.stdout_lines[-1]


def test_overlay_check_effect_contract_binds_the_command_line_and_its_topics() -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text(encoding="utf-8"))

    assert contract["event_bus"]["subscribe_topics"] == [_COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [_COMPLETED_TOPIC]
    assert contract["runtime_dispatch"]["command_topic"] == _COMMAND_TOPIC
    assert contract["entry_points"]["console_scripts"] == {
        "onex-automation-liveness-check": (
            "omnimarket.nodes.node_automation_liveness_overlay_check_effect."
            "runtime_automation_liveness_overlay_check_cli:main"
        )
    }
