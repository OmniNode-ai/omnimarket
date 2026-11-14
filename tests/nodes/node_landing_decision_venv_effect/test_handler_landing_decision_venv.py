# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler cases of the landing decision venv effect.

The runner is a model of a venv for the install cases; the last cases run the real subprocess adapter and a real venv.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_landing_decision_venv_effect.handlers.handler_landing_decision_venv import (
    HandlerLandingDecisionVenv,
)
from omnimarket.nodes.node_landing_decision_venv_effect.handlers.handler_subprocess_runner import (
    HandlerSubprocessRunner,
)
from omnimarket.nodes.node_landing_decision_venv_effect.models.model_landing_decision_venv import (
    ModelLandingDecisionVenvOverlay,
    ModelLandingDecisionVenvRequest,
)
from tests.nodes.node_landing_decision_venv_effect.conftest import (
    PIN,
    PIP_TIMEOUT,
    PROBE_TIMEOUT,
    VENV_TIMEOUT,
    FakeVenvRunner,
    make_overlay,
)

pytestmark = pytest.mark.unit

MakeRequest = Callable[..., ModelLandingDecisionVenvRequest]


def test_a_first_run_creates_the_venv_and_installs_every_pinned_dependency(
    make_request: MakeRequest,
) -> None:
    runner = FakeVenvRunner()
    result = HandlerLandingDecisionVenv(overlay=make_overlay(), runner=runner).handle(
        make_request()
    )
    assert result.outcome == "installed"
    assert result.reason == ""
    assert result.versions == {
        "pydantic": PIN["pydantic"],
        "pydantic-core": PIN["pydantic_core"],
        "pyyaml": PIN["pyyaml"],
    }
    assert runner.steps() == ["venv", "probe", "pip", "probe"]


def test_a_second_run_reads_ready_and_installs_nothing(
    make_request: MakeRequest,
) -> None:
    runner = FakeVenvRunner()
    handler = HandlerLandingDecisionVenv(overlay=make_overlay(), runner=runner)
    assert handler.handle(make_request()).outcome == "installed"
    runner.calls.clear()
    second = handler.handle(make_request())
    assert second.outcome == "ready"
    assert second.versions["pyyaml"] == PIN["pyyaml"]
    assert runner.steps() == ["probe"]


def test_the_timeouts_come_from_the_overlay_and_not_the_handler(
    make_request: MakeRequest,
) -> None:
    runner = FakeVenvRunner()
    HandlerLandingDecisionVenv(overlay=make_overlay(), runner=runner).handle(
        make_request()
    )
    assert runner.calls == [
        ("venv", VENV_TIMEOUT),
        ("probe", PROBE_TIMEOUT),
        ("pip", PIP_TIMEOUT),
        ("probe", PROBE_TIMEOUT),
    ]


def test_failing_control_an_overlay_with_no_pyyaml_row_leaves_the_venv_without_it(
    make_request: MakeRequest,
) -> None:
    """Control: the pin names pyyaml in both runs; only the overlay's row decides whether the venv gets it."""
    rows = {"pydantic": "pydantic", "pydantic_core": "pydantic-core"}
    without = FakeVenvRunner()
    result = HandlerLandingDecisionVenv(
        overlay=make_overlay(rows), runner=without
    ).handle(make_request())
    assert result.outcome == "installed"
    assert "pyyaml" not in without.installed
    assert "pyyaml" not in result.versions

    with_row = FakeVenvRunner()
    HandlerLandingDecisionVenv(overlay=make_overlay(), runner=with_row).handle(
        make_request()
    )
    assert with_row.installed["pyyaml"] == PIN["pyyaml"]


def test_a_row_installs_the_distribution_it_maps_not_the_pin_key(
    make_request: MakeRequest,
) -> None:
    runner = FakeVenvRunner()
    HandlerLandingDecisionVenv(overlay=make_overlay(), runner=runner).handle(
        make_request()
    )
    assert "pydantic-core" in runner.installed
    assert "pydantic_core" not in runner.installed


def test_the_overlay_model_refuses_what_it_does_not_name() -> None:
    document = {
        "distributions": {"pyyaml": "pyyaml"},
        "venv_args": ["-m", "venv"],
        "pip_args": ["-m", "pip", "install"],
        "venv_timeout_seconds": 1,
        "pip_timeout_seconds": 1,
        "probe_timeout_seconds": 1,
    }
    ModelLandingDecisionVenvOverlay.model_validate(document)
    for broken in (
        {**document, "extra": 1},
        {**document, "distributions": {}},
        {**document, "pip_timeout_seconds": 0},
        {k: v for k, v in document.items() if k != "venv_args"},
    ):
        with pytest.raises(ValidationError):
            ModelLandingDecisionVenvOverlay.model_validate(broken)


def test_the_overlay_is_found_under_the_overlay_roots(
    make_request: MakeRequest, tmp_path: Path
) -> None:
    """The handler reads ``<root>/node_landing_decision_venv_effect/overlay.yaml`` when none is given."""
    node_dir = tmp_path / "roots" / "node_landing_decision_venv_effect"
    node_dir.mkdir(parents=True)
    (node_dir / "overlay.yaml").write_text(
        yaml.safe_dump(make_overlay().model_dump()), encoding="utf-8"
    )
    environ = {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "roots")}
    runner = FakeVenvRunner()
    result = HandlerLandingDecisionVenv(runner=runner, environ=environ).handle(
        make_request()
    )
    assert result.outcome == "installed"
    assert runner.calls[0] == ("venv", VENV_TIMEOUT)


def test_the_real_adapter_reports_a_timeout_and_a_command_that_never_started() -> None:
    adapter = HandlerSubprocessRunner()
    slow = adapter.run([sys.executable, "-c", "import time; time.sleep(30)"], 0.3)
    assert slow.timed_out is True
    missing = adapter.run(["/no/such/interpreter"], 5)
    assert missing.timed_out is False
    assert missing.os_error == "FileNotFoundError"
    fine = adapter.run([sys.executable, "-c", "print('ok')"], 30)
    assert (fine.returncode, fine.stdout.strip(), fine.timed_out) == (0, "ok", False)


def test_a_real_venv_is_created_and_an_install_that_adds_nothing_fails_the_read_back(
    make_request: MakeRequest,
) -> None:
    """Real interpreter, real venv, no network: the install command here runs ``-c pass``, so nothing installs."""
    overlay = ModelLandingDecisionVenvOverlay(
        distributions={"pyyaml": "pyyaml"},
        venv_args=["-m", "venv", "--without-pip"],
        pip_args=["-c", "pass"],
        venv_timeout_seconds=120,
        pip_timeout_seconds=30,
        probe_timeout_seconds=30,
    )
    request = make_request().model_copy(update={"base_python": sys.executable})
    result = HandlerLandingDecisionVenv(overlay=overlay).handle(request)
    assert (Path(request.venv_dir) / "bin" / "python").exists()
    assert result.outcome == "refused"
    assert result.reason == "verify-failed:pyyaml"
