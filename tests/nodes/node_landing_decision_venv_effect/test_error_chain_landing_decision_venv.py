# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chains of the landing decision venv node (OMN-24355).

Each failure ends as a refused result with a typed reason and removes the runtime file the controller reads;
none raises.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from omnimarket.nodes.node_landing_decision_venv_effect.handlers import (
    HandlerLandingDecisionVenv,
)
from omnimarket.nodes.node_landing_decision_venv_effect.models import (
    ModelLandingDecisionVenvRequest,
)

from .conftest import PIN, FakeVenvRunner, make_overlay, write_overlay_root

pytestmark = pytest.mark.unit

Make = Callable[..., ModelLandingDecisionVenvRequest]


def _refused_reason(
    request: ModelLandingDecisionVenvRequest, runner: FakeVenvRunner
) -> str:
    result = HandlerLandingDecisionVenv(runner, overlay=make_overlay()).handle(request)
    assert result.status == "refused", result
    assert result.reasons
    return result.reason


@pytest.mark.parametrize(
    ("runner_kwargs", "reason"),
    [
        ({"fail": "venv"}, "venv-create-failed:rc=1"),
        ({"os_error": "FileNotFoundError"}, "venv-create-failed:FileNotFoundError"),
        ({"fail": "pip"}, "pip-install-failed:rc=1"),
        ({"hang": "venv"}, "timeout:venv-create"),
        ({"hang": "pip"}, "timeout:pip-install"),
        ({"hang": "probe"}, "timeout:probe"),
        ({"refuse_pip_for": "pyyaml"}, "version-mismatch:pyyaml"),
    ],
)
def test_error_chain_a_failed_step_is_a_typed_refusal(
    make_build_request: Make, runner_kwargs: dict[str, str], reason: str
) -> None:
    assert (
        _refused_reason(make_build_request(), FakeVenvRunner(**runner_kwargs)) == reason
    )


def test_error_chain_a_pin_missing_a_required_dependency_is_refused_before_any_command(
    make_build_request: Make, pin_file: Path
) -> None:
    pin = {key: value for key, value in PIN.items() if key != "pyyaml"}
    pin_file.write_text(json.dumps(pin), encoding="utf-8")
    runner = FakeVenvRunner()
    assert _refused_reason(make_build_request(), runner) == "pin-missing:pyyaml"
    assert runner.calls == []


def test_error_chain_a_pin_version_that_is_not_exact_is_refused(
    make_build_request: Make, pin_file: Path
) -> None:
    pin_file.write_text(
        json.dumps({**PIN, "pyyaml": "6.0; rm -rf /"}), encoding="utf-8"
    )
    runner = FakeVenvRunner()
    assert _refused_reason(make_build_request(), runner) == "pin-invalid:pyyaml"
    assert runner.calls == []


def test_error_chain_an_unreadable_pin_is_refused(
    make_build_request: Make, tmp_path: Path
) -> None:
    request = make_build_request(tmp_path / "absent.json")
    assert _refused_reason(request, FakeVenvRunner()) == "pin-unreadable"


def test_error_chain_a_failed_command_removes_the_runtime_file(
    tmp_path: Path, pin_file: Path
) -> None:
    runtime = tmp_path / "state" / "landing_runtime.json"
    runtime.parent.mkdir()
    runtime.write_text("{}", encoding="utf-8")
    request = ModelLandingDecisionVenvRequest(
        operation="build",
        pin_file=pin_file,
        venv_dir=tmp_path / "decision-venv",
        base_python=Path("/base/python3"),
        node_src=tmp_path / "src",
        runtime_file=runtime,
    )
    result = HandlerLandingDecisionVenv(
        FakeVenvRunner(fail="pip"), overlay=make_overlay()
    ).handle(request)
    assert result.status == "refused"
    assert not runtime.exists()


def test_error_chain_an_absent_overlay_is_refused(
    make_build_request: Make, tmp_path: Path
) -> None:
    runner = FakeVenvRunner()
    result = HandlerLandingDecisionVenv(
        runner, environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "no-root")}
    ).handle(make_build_request())
    assert (result.status, result.reason) == ("refused", "overlay-not-found")
    assert runner.calls == []


@pytest.mark.parametrize(
    "text", ["distributions: {pydantic: pydantic}\n", ": not yaml: [", "- a\n- b\n"]
)
def test_error_chain_a_broken_overlay_is_refused(
    make_build_request: Make, tmp_path: Path, text: str
) -> None:
    root = write_overlay_root(tmp_path / "overlays", text)
    runner = FakeVenvRunner()
    result = HandlerLandingDecisionVenv(
        runner, environ={"ONEX_SKILL_OVERLAY_ROOTS": str(root)}
    ).handle(make_build_request())
    assert (result.status, result.reason) == ("refused", "overlay-broken")
    assert runner.calls == []


def test_error_chain_the_overlay_file_is_read_through_the_roots(
    make_build_request: Make, tmp_path: Path
) -> None:
    root = write_overlay_root(tmp_path / "overlays")
    result = HandlerLandingDecisionVenv(
        FakeVenvRunner(), environ={"ONEX_SKILL_OVERLAY_ROOTS": str(root)}
    ).handle(make_build_request())
    assert result.status == "built", result.reasons
