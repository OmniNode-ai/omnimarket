# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The retain_run_artifacts pin guard (OMN-22848).

The pinned omnibase_spi has no ``ModelLaneRetainRunArtifactsRequest`` yet (it lands in
omnibase_spi#456), so the ssh transport must keep answering the step "not run" without any host
command. Once the pin carries the model, a transport that still answers "not run" must turn this
test red, so the step gets implemented with the spi model.
"""

from __future__ import annotations

import types
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import omnibase_spi.contracts.services
import pytest
from omnibase_spi.contracts.services import (
    ModelLaneHostRef,
    ModelLaneHostStepRequest,
    ModelLaneHostStepResult,
)

from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_scripted_transport import (
    ModelScriptedTransportScript,
    ScriptedLaneHostTransport,
)
from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_ssh_transport import (
    STEP_NOT_RUN_BY_NODE,
    ModelHostCommandResult,
    SshLaneHostTransport,
)

pytestmark = [pytest.mark.unit]

REQUEST = ModelLaneHostStepRequest(
    host=ModelLaneHostRef(name="h106", target="h106"),
    lane="l",
    run_id="r",
    timeout_seconds=5,
)
#: The STEP_NOT_RUN_BY_NODE text after its ``{op}`` slot: the suffix a not-run answer must end with.
_NOT_RUN_SUFFIX = STEP_NOT_RUN_BY_NODE.split("{op}", 1)[1]


class Recorder:
    """The host command runner that records every argv and answers with one fixed result."""

    def __init__(self, answer: ModelHostCommandResult) -> None:
        self.answer = answer
        self.argvs: list[list[str]] = []

    def run(
        self,
        argv: Sequence[str],
        timeout_seconds: float,
        stdin: bytes | None = None,
    ) -> ModelHostCommandResult:
        self.argvs.append(list(argv))
        return self.answer


def transport(runner: Recorder, root: str) -> SshLaneHostTransport:
    return SshLaneHostTransport(
        ssh_argv=("ssh",),
        host_run_root=root,
        call_timeout_seconds=30,
        detach_argv=("setsid",),
        runner=runner,
    )


def retain_model_in(services: ModuleType) -> bool:
    return hasattr(services, "ModelLaneRetainRunArtifactsRequest")


def assert_retain_is_declared_not_run(
    services: ModuleType, step_result: ModelLaneHostStepResult
) -> None:
    is_not_run = (
        step_result.ok is False
        and step_result.error is not None
        and step_result.error.endswith(_NOT_RUN_SUFFIX)
    )
    if retain_model_in(services) and is_not_run:
        raise AssertionError(
            "the pinned omnibase_spi carries ModelLaneRetainRunArtifactsRequest: implement "
            "retain_run_artifacts in the ssh transport with the spi model instead of "
            "answering the step not run"
        )
    if not retain_model_in(services) and not is_not_run:
        raise AssertionError(
            "the pinned omnibase_spi has no ModelLaneRetainRunArtifactsRequest: the step must "
            "not pretend to run before the model exists"
        )


def test_as_pinned_retain_run_artifacts_is_declared_not_run(tmp_path: Path) -> None:
    runner = Recorder(ModelHostCommandResult(exit_code=0))
    result = transport(runner, root=str(tmp_path / "runs")).retain_run_artifacts(
        REQUEST
    )
    assert runner.argvs == []
    assert_retain_is_declared_not_run(omnibase_spi.contracts.services, result)


def test_the_day_the_pin_carries_the_model_the_guard_turns_red() -> None:
    pinned = types.ModuleType("fake_services")
    vars(pinned)["ModelLaneRetainRunArtifactsRequest"] = object
    not_run = ModelLaneHostStepResult(
        ok=False, error=STEP_NOT_RUN_BY_NODE.format(op="retain_run_artifacts")
    )
    with pytest.raises(AssertionError, match="implement"):
        assert_retain_is_declared_not_run(pinned, not_run)

    absent = types.ModuleType("fake_services_absent")
    with pytest.raises(AssertionError, match="must not pretend to run"):
        assert_retain_is_declared_not_run(
            absent, ModelLaneHostStepResult(ok=True, detail="ran")
        )


def test_the_scripted_transport_answers_ok_for_retain() -> None:
    scripted = ScriptedLaneHostTransport(ModelScriptedTransportScript())
    assert scripted.retain_run_artifacts(REQUEST).ok is True


def test_retain_is_routed_through_the_effect_handler_and_stays_not_run(
    tmp_path: Path,
) -> None:
    from omnimarket.models.remote_lane_host_call.model_remote_lane_host_call import (
        EnumLaneHostOperation,
        ModelRemoteLaneHostCall,
    )
    from omnimarket.nodes.node_remote_lane_start_effect.handlers.handler_remote_lane_start_effect import (
        HandlerRemoteLaneStartEffect,
    )

    runner = Recorder(ModelHostCommandResult(exit_code=0))
    handler = HandlerRemoteLaneStartEffect(
        transport(runner, root=str(tmp_path / "runs"))
    )
    answer = handler.handle(
        ModelRemoteLaneHostCall(
            operation=EnumLaneHostOperation.RETAIN_RUN_ARTIFACTS, step=REQUEST
        )
    )
    assert runner.argvs == []
    assert answer.step_result is not None
    assert_retain_is_declared_not_run(
        omnibase_spi.contracts.services, answer.step_result
    )
