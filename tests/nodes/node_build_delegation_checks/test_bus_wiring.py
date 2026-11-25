# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract-bound handler, built with no argument, runs a build from the overlay and the registry root and publishes the three
output topics its contract declares."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_build_delegation_checks.adapters.bus_sink import (
    BusRegressionSink,
)
from omnimarket.nodes.node_build_delegation_checks.handlers.handler_build_delegation_checks import (
    OMNI_HOME_ENV,
    REPLAY_ENV,
    HandlerBuildDelegationChecks,
)
from omnimarket.nodes.node_build_delegation_checks.models.model_build_delegation_checks import (
    EnumBuildChecksStatus,
    ModelRebuildCompletedObserved,
)
from omnimarket.nodes.node_build_delegation_checks.overlay import FILE_ENV

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_build_delegation_checks"
)
REGRESSION_TOPIC = "onex.evt.omnimarket.delegation-check-regression-detected.v1"
ALARM_TOPIC = "onex.cmd.omnimarket.lab-alarm-delivery-requested.v1"
RESULT_TOPIC = "onex.evt.omnimarket.build-delegation-checks-recorded.v1"
CHECK_TOPIC = "onex.evt.omnimarket.build-delegation-check-recorded.v1"


def _git(clone: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-c",
            "user.name=lane",
            "-c",
            "user.email=lane@example.invalid",
            "-C",
            str(clone),
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    return done.stdout.strip()


def _recorded(path: Path, green: str, red: str) -> None:
    def row(reading: str) -> dict[str, str]:
        return {"reading": reading, "run_id": f"run-{reading}"}

    path.write_text(
        json.dumps(
            {
                "builds": {
                    green: {"C14": row("pass"), "C29": row("pass")},
                    red: {"C14": row("fail"), "C29": row("pass")},
                }
            }
        ),
        encoding="utf-8",
    )


def test_the_contract_declares_the_four_output_topics() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["event_bus"]["publish_topics"] == [
        REGRESSION_TOPIC,
        ALARM_TOPIC,
        RESULT_TOPIC,
        CHECK_TOPIC,
    ]
    assert contract["handler"]["class"] == "HandlerBuildDelegationChecks"
    assert contract["handler"]["module"].endswith(
        ".handlers.handler_build_delegation_checks"
    )


def test_the_no_argument_handler_publishes_each_output_topic_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "omnimarket"
    clone.mkdir()
    _git(clone, "init", "-q")
    _git(clone, "commit", "-q", "--allow-empty", "-m", "first (#1)")
    green = _git(clone, "rev-parse", "HEAD")
    _git(clone, "commit", "-q", "--allow-empty", "-m", "second (#2)")
    red = _git(clone, "rev-parse", "HEAD")
    replay = tmp_path / "recorded.json"
    _recorded(replay, green, red)
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        yaml.safe_dump({"checks": ["C14", "C29"], "owner": "delegation-owner"}),
        encoding="utf-8",
    )
    monkeypatch.setenv(OMNI_HOME_ENV, str(tmp_path))
    monkeypatch.setenv(REPLAY_ENV, str(replay))
    monkeypatch.setenv(FILE_ENV, str(overlay))
    sent: list[tuple[str, str]] = []

    async def capture(
        self: BusRegressionSink, topic: str, key: str, value: bytes
    ) -> None:
        sent.append((topic, json.loads(value)["payload"].get("build_id", key)))

    monkeypatch.setattr(BusRegressionSink, "_send", capture)

    handler = HandlerBuildDelegationChecks()
    first = handler.handle(
        ModelRebuildCompletedObserved(git_sha=green, runtime_lane="dev")
    )
    second = handler.handle(
        ModelRebuildCompletedObserved(git_sha=red, runtime_lane="dev")
    )
    replayed = handler.handle(
        ModelRebuildCompletedObserved(git_sha=red, runtime_lane="dev")
    )

    assert first.status != EnumBuildChecksStatus.ALREADY_RUN
    assert second.status != EnumBuildChecksStatus.ALREADY_RUN
    assert replayed.status == EnumBuildChecksStatus.ALREADY_RUN
    topics = [topic for topic, _ in sent]
    assert topics.count(REGRESSION_TOPIC) == 1
    assert topics.count(ALARM_TOPIC) == 1
    assert topics.count(RESULT_TOPIC) == 2
    # One record per check on the per-check topic: the overlay names two checks and two builds ran.
    assert topics.count(CHECK_TOPIC) == 4


def test_a_partial_set_of_ports_is_refused() -> None:
    from omnimarket.models.overlay_documents.model_build_delegation_checks_overlay import (
        ModelBuildDelegationChecksOverlay,
    )

    with pytest.raises(ValueError, match="every port"):
        HandlerBuildDelegationChecks(settings=ModelBuildDelegationChecksOverlay())
