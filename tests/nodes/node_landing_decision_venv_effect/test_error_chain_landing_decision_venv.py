# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain event tests of the landing decision venv effect.

Inject each defined fault and assert the one typed failure event; a raise would leave no event and fail the wait.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from omnimarket.nodes.node_landing_decision_venv_effect.handlers.handler_landing_decision_venv import (
    HandlerLandingDecisionVenv,
)
from omnimarket.nodes.node_landing_decision_venv_effect.models.model_landing_decision_venv import (
    ModelLandingDecisionVenvRequest,
)
from tests.nodes.node_landing_decision_venv_effect.conftest import (
    CORRELATION,
    PIN,
    FakeVenvRunner,
    make_overlay,
)
from tests.nodes.node_landing_decision_venv_effect.test_golden_chain_landing_decision_venv import (
    TOPICS,
    publish,
    request_payload,
)

pytestmark = pytest.mark.unit

MakeRequest = Callable[..., ModelLandingDecisionVenvRequest]


def failed_reason(
    handler: HandlerLandingDecisionVenv, request: ModelLandingDecisionVenvRequest
) -> str:
    events = asyncio.run(publish(request_payload(request), handler))
    assert [topic for topic, _ in events] == [
        TOPICS["landing_decision_venv_run_failed"]
    ]
    assert events[0][1]["correlation_id"] == CORRELATION
    reason = events[0][1]["failure"]
    assert isinstance(reason, str)
    return reason


def test_error_chain_a_pin_without_a_required_dependency_is_refused(
    make_request: MakeRequest, tmp_path: Path
) -> None:
    pin = tmp_path / "short.pin.json"
    pin.write_text(
        json.dumps({k: v for k, v in PIN.items() if k != "pyyaml"}), encoding="utf-8"
    )
    runner = FakeVenvRunner()
    handler = HandlerLandingDecisionVenv(overlay=make_overlay(), runner=runner)
    assert failed_reason(handler, make_request(pin)) == "pin-missing:pyyaml"
    assert runner.calls == []


def test_error_chain_a_pin_that_cannot_be_read_is_refused(
    make_request: MakeRequest, tmp_path: Path
) -> None:
    broken = tmp_path / "broken.pin.json"
    broken.write_text("{not json", encoding="utf-8")
    handler = HandlerLandingDecisionVenv(
        overlay=make_overlay(), runner=FakeVenvRunner()
    )
    assert failed_reason(handler, make_request(broken)) == "pin-unreadable"
    assert (
        failed_reason(handler, make_request(tmp_path / "absent.json"))
        == "pin-unreadable"
    )


def test_error_chain_a_failed_venv_create_is_refused(make_request: MakeRequest) -> None:
    handler = HandlerLandingDecisionVenv(
        overlay=make_overlay(), runner=FakeVenvRunner(fail="venv")
    )
    assert failed_reason(handler, make_request()) == "venv-create-failed:rc=1"


def test_error_chain_a_base_interpreter_that_cannot_start_is_refused(
    make_request: MakeRequest,
) -> None:
    handler = HandlerLandingDecisionVenv(
        overlay=make_overlay(), runner=FakeVenvRunner(os_error="FileNotFoundError")
    )
    assert (
        failed_reason(handler, make_request()) == "venv-create-failed:FileNotFoundError"
    )


def test_error_chain_a_failed_pip_install_is_refused(make_request: MakeRequest) -> None:
    handler = HandlerLandingDecisionVenv(
        overlay=make_overlay(), runner=FakeVenvRunner(fail="pip")
    )
    assert failed_reason(handler, make_request()) == "pip-install-failed:rc=1"


@pytest.mark.parametrize(
    ("step", "reason"),
    [
        ("venv", "timeout:venv-create"),
        ("pip", "timeout:pip-install"),
        ("probe", "timeout:probe"),
    ],
)
def test_error_chain_a_timeout_is_refused(
    make_request: MakeRequest, step: str, reason: str
) -> None:
    handler = HandlerLandingDecisionVenv(
        overlay=make_overlay(), runner=FakeVenvRunner(hang=step)
    )
    assert failed_reason(handler, make_request()) == reason


def test_error_chain_an_install_that_leaves_a_dependency_out_is_refused(
    make_request: MakeRequest,
) -> None:
    handler = HandlerLandingDecisionVenv(
        overlay=make_overlay(), runner=FakeVenvRunner(refuse_pip_for="pyyaml")
    )
    assert failed_reason(handler, make_request()) == "verify-failed:pyyaml"


def test_error_chain_an_overlay_that_is_absent_is_refused(
    make_request: MakeRequest, tmp_path: Path
) -> None:
    handler = HandlerLandingDecisionVenv(
        runner=FakeVenvRunner(),
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "none")},
    )
    assert failed_reason(handler, make_request()) == "overlay-not-found"


def test_error_chain_an_overlay_that_is_broken_is_refused(
    make_request: MakeRequest, tmp_path: Path
) -> None:
    node_dir = tmp_path / "roots" / "node_landing_decision_venv_effect"
    node_dir.mkdir(parents=True)
    (node_dir / "overlay.yaml").write_text(
        "distributions: {}\nsurprise: 1\n", encoding="utf-8"
    )
    runner = FakeVenvRunner()
    handler = HandlerLandingDecisionVenv(
        runner=runner, environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "roots")}
    )
    assert failed_reason(handler, make_request()) == "overlay-broken"
    assert runner.calls == []


def test_error_chain_a_payload_that_is_not_a_request_gives_one_failed_event() -> None:
    handler = HandlerLandingDecisionVenv(
        overlay=make_overlay(), runner=FakeVenvRunner()
    )
    events = asyncio.run(publish(b'{"pin_path": 7}', handler))
    assert [topic for topic, _ in events] == [
        TOPICS["landing_decision_venv_run_failed"]
    ]
    assert events[0][1]["failure"].startswith("bad-payload:")
