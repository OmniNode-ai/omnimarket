# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain and failing control of the landing decision venv node (OMN-24355).

The golden chain is the request to the result through the real handler, with the fake runner standing in for
the subprocesses: a build installs every dependency the pin names, the overlay's row for pyyaml included, and
a second build finds them in place and reads ready.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from omnimarket.nodes.node_landing_decision_venv_effect.handlers import (
    HandlerLandingDecisionVenv,
)
from omnimarket.nodes.node_landing_decision_venv_effect.models import (
    ModelLandingDecisionVenvRequest,
)

from .conftest import (
    PIN,
    PIP_TIMEOUT,
    PROBE_TIMEOUT,
    VENV_TIMEOUT,
    FakeVenvRunner,
    make_overlay,
)

pytestmark = pytest.mark.unit

Make = Callable[..., ModelLandingDecisionVenvRequest]


def test_golden_chain_installs_every_pinned_dependency_then_reads_ready(
    make_build_request: Make,
) -> None:
    runner = FakeVenvRunner()
    handler = HandlerLandingDecisionVenv(runner, overlay=make_overlay())
    first = handler.handle(make_build_request())
    assert first.status == "built", first.reasons
    assert runner.installed == {
        "pydantic": PIN["pydantic"],
        "pydantic-core": PIN["pydantic_core"],
        "pyyaml": PIN["pyyaml"],
    }
    assert first.installed == (
        f"pydantic=={PIN['pydantic']}",
        f"pydantic-core=={PIN['pydantic_core']}",
        f"pyyaml=={PIN['pyyaml']}",
    )
    second = handler.handle(make_build_request())
    assert second.status == "ready", second.reasons
    assert second.installed == first.installed
    assert runner.steps() == ["venv", "pip", "probe", "probe", "probe"]
    assert runner.steps().count("pip") == 1, "the second run installed nothing"


def test_golden_chain_the_timeouts_come_from_the_overlay(
    make_build_request: Make,
) -> None:
    runner = FakeVenvRunner()
    HandlerLandingDecisionVenv(runner, overlay=make_overlay()).handle(
        make_build_request()
    )
    timeouts = dict(runner.calls)
    assert timeouts["venv"] == VENV_TIMEOUT
    assert timeouts["pip"] == PIP_TIMEOUT
    assert timeouts["probe"] == PROBE_TIMEOUT


def test_failing_control_an_overlay_without_the_row_leaves_the_venv_without_the_dependency(
    make_build_request: Make,
) -> None:
    """The same pin, one overlay with the pyyaml row and one without: only the row installs it."""
    rows = {"pydantic": "pydantic", "pydantic_core": "pydantic-core"}
    without = FakeVenvRunner()
    result = HandlerLandingDecisionVenv(without, overlay=make_overlay(rows)).handle(
        make_build_request()
    )
    assert result.status == "built", result.reasons
    assert "pyyaml" not in without.installed
    with_row = FakeVenvRunner()
    HandlerLandingDecisionVenv(with_row, overlay=make_overlay()).handle(
        make_build_request()
    )
    assert with_row.installed["pyyaml"] == PIN["pyyaml"]


def test_failing_control_the_row_is_what_makes_a_missing_pin_key_refuse(
    make_build_request: Make, pin_file: Path
) -> None:
    """With the row and a pin that lacks its key the request is refused; without the row it is not."""
    pin_file.write_text(
        '{"commit": "50ad28a50753e3c7e5cc1b63abd2d0768c16c364", '
        '"pydantic": "2.13.5", "pydantic_core": "2.46.5"}',
        encoding="utf-8",
    )
    refused = HandlerLandingDecisionVenv(
        FakeVenvRunner(), overlay=make_overlay()
    ).handle(make_build_request())
    assert refused.status == "refused"
    assert refused.reason == "pin-missing:pyyaml"
    rows = {"pydantic": "pydantic", "pydantic_core": "pydantic-core"}
    built = HandlerLandingDecisionVenv(
        FakeVenvRunner(), overlay=make_overlay(rows)
    ).handle(make_build_request())
    assert built.status == "built"
