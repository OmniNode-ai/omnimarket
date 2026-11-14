# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A deployment may declare the drain verb alone: runner_stop is optional, and the runner-stop verb refuses without it (OMN-24489)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_host_drain_effect.overlay import (
    EXAMPLE_OVERLAY,
    ModelHostDrainOverlay,
)
from omnimarket.nodes.node_host_drain_effect.runtime_host_drain import run


def _drain_only() -> Mapping[str, Any]:
    raw = yaml.safe_load(EXAMPLE_OVERLAY.read_text(encoding="utf-8"))
    del raw["runner_stop"]
    return raw  # type: ignore[no-any-return]


def test_overlay_without_runner_stop_loads_and_names_no_runner() -> None:
    overlay = ModelHostDrainOverlay.model_validate(_drain_only())
    assert overlay.runner_stop is None
    assert overlay.runner("example-runner-1") is None


def test_runner_stop_verb_refuses_when_the_overlay_declares_none(
    capsys: pytest.CaptureFixture[str],
) -> None:
    overlay = ModelHostDrainOverlay.model_validate(_drain_only())
    assert (
        run(
            ["runner-stop", "--runner", "example-runner-1"], overlay=overlay, environ={}
        )
        == 3
    )
    assert "declares no runner_stop" in capsys.readouterr().err


def test_drain_status_line_verb_prints_the_line_not_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    overlay = ModelHostDrainOverlay.model_validate(_drain_only())
    code = run(
        ["status", "--host", "example-host-a", "--line"], overlay=overlay, environ={}
    )
    out = capsys.readouterr().out.strip()
    # The example overlay's lane count command does not exist on this host: the count is unreadable, never zero.
    assert code == 0
    assert out == "example-host-a not draining: lane count unreadable"


def test_a_refused_drain_prints_its_code_and_detail_as_the_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    overlay = ModelHostDrainOverlay.model_validate(_drain_only())
    code = run(
        ["drain", "--host", "no-such-host", "--line"], overlay=overlay, environ={}
    )
    out = capsys.readouterr().out.strip()
    assert code == 1
    assert (
        out
        == f"no-such-host: {overlay.drain.codes.unknown_host}: no-such-host is not a declared host"
    )
