# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain events of node_host_bringup_run_effect.

Golden chain: every step ok publishes the completed event, with the run's result as its body.
Error chain: a failed step publishes the failed event, runs nothing after it and names the
rollback. Both topics are read from the node contract, never typed here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_host_bringup_run_effect.handlers import (
    handler_host_bringup_run as handler_module,
)
from omnimarket.nodes.node_host_bringup_run_effect.handlers.adapter_installer import (
    InMemoryDeployStore,
    InMemoryInstaller,
    InMemoryPublisher,
)
from omnimarket.nodes.node_host_bringup_run_effect.handlers.handler_host_bringup_run import (
    HandlerHostBringupRun,
)
from omnimarket.nodes.node_host_bringup_run_effect.models import (
    EnumBringupMode,
    EnumStepOutcome,
    ModelHostBringupRunOverlay,
    ModelHostBringupRunRequest,
    ModelHostBringupRunResult,
)

pytestmark = pytest.mark.unit

CONTRACT = yaml.safe_load(
    (Path(handler_module.__file__).resolve().parents[1] / "contract.yaml").read_text(
        encoding="utf-8"
    )
)
STEP = {"rollback": "none", "proof": "the read that shows it current"}
OVERLAY = ModelHostBringupRunOverlay.model_validate(
    {
        "installer_command": ["installer"],
        "repo_root": "/clone",
        "roles": {
            "linux-lab": {
                "steps": [
                    {"unit": "unit-a", "host_name": True, **STEP},
                    {"unit": "unit-b", **STEP},
                    {"unit": "unit-c", **STEP},
                ]
            }
        },
    }
)


def run_chain(
    installer: InMemoryInstaller,
) -> tuple[InMemoryPublisher, ModelHostBringupRunResult]:
    publisher = InMemoryPublisher()
    handler = HandlerHostBringupRun(
        overlay=OVERLAY,
        installer=installer,
        deploy_store=InMemoryDeployStore(),
        publisher=publisher,
        environ={},
    )
    result = handler.handle(
        ModelHostBringupRunRequest(host_name="h-new", mode=EnumBringupMode.INSTALL)
    )
    return publisher, result


def assert_published_on_failed_topic(publisher: InMemoryPublisher) -> None:
    """The reading: a failed run's one event is on the contract's failure topic."""
    topics = [topic for topic, _event in publisher.events]
    assert topics == [CONTRACT["failure_event"]], f"a failed run published on {topics}"


def test_golden_chain_every_step_ok_publishes_the_completed_event() -> None:
    installer = InMemoryInstaller()
    publisher, result = run_chain(installer)
    assert [topic for topic, _event in publisher.events] == [CONTRACT["terminal_event"]]
    assert publisher.events[0][1] == result
    assert (result.completed, result.exit_code, result.stopped_at) == (True, 0, None)
    assert installer.calls == [
        ("unit-a", "install", "h-new"),
        ("unit-b", "install", None),
        ("unit-c", "install", None),
    ]
    assert [step.outcome for step in result.steps] == [EnumStepOutcome.OK] * 3
    assert all(step.proof for step in result.steps)


def test_error_chain_failed_step_publishes_the_failed_event_and_stops() -> None:
    installer = InMemoryInstaller(fail={"unit-b": 7})
    publisher, result = run_chain(installer)
    assert_published_on_failed_topic(publisher)
    assert publisher.events[0][1] == result
    assert (result.completed, result.exit_code, result.stopped_at) == (False, 7, 2)
    assert [call[0] for call in installer.calls] == ["unit-a", "unit-b"]
    assert result.steps[-1].outcome is EnumStepOutcome.FAILED
    assert result.rollback_hint is not None
    assert "rollback" in result.rollback_hint


def test_failing_control_a_failed_run_never_publishes_the_completed_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Injected break completed_topic_for_all: the failed run goes out on the completed topic."""
    monkeypatch.setattr(handler_module, "FAILED_TOPIC", handler_module.COMPLETED_TOPIC)
    publisher, _result = run_chain(InMemoryInstaller(fail={"unit-b": 7}))
    with pytest.raises(AssertionError, match="a failed run published on"):
        assert_published_on_failed_topic(publisher)
