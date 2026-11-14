# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab_work backend of the GPU power caps node (OMN-24078): one lab work unit per request, the host's result line back.

Shape followed: test_gpu_power_caps.py in this directory (a fake injected at the port, a failing control per reason).
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.model_lab_work_unit import (
    EnumLabWorkUnitStatus,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_gpu_power_caps_effect.handlers.handler_gpu_power_caps import (
    HandlerGpuPowerCaps,
    select_backend,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps import (
    RESULT_LINE_PREFIX,
    EnumGpuPowerCapsBackend,
    EnumGpuPowerCapsCardAction,
    EnumGpuPowerCapsOutcome,
    EnumGpuPowerCapsRefusal,
    ModelGpuPowerCapsCardResult,
    ModelGpuPowerCapsRequest,
    ModelGpuPowerCapsResult,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps_overlay import (
    ModelGpuPowerCapsOverlay,
)
from tests.unit.nodes.node_gpu_power_caps_effect.fixtures import NODE_DIR, SIM_A

pytestmark = pytest.mark.unit

EXAMPLE = NODE_DIR / "overlays/lab_work.example.yaml"


def overlay_document() -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    doc["hosts"] = [
        {"name": SIM_A, "transport_prefix": [], "caps_unit": "gpu-power-caps-sim-a"}
    ]
    return doc


def lab_work_overlay() -> ModelGpuPowerCapsOverlay:
    return ModelGpuPowerCapsOverlay.model_validate(overlay_document())


class FakeSender:
    """The sender port: records every unit and answers with the receipt the test set."""

    def __init__(self, receipt: ModelLabWorkUnitReceipt | None = None) -> None:
        self.calls: list[ModelLabWorkUnitRequest] = []
        self.receipt = receipt

    def send(self, request: ModelLabWorkUnitRequest) -> ModelLabWorkUnitReceipt:
        self.calls.append(request)
        assert self.receipt is not None
        return self.receipt


def receipt(
    status: EnumLabWorkUnitStatus,
    *,
    tail: str = "",
    exit_code: int | None = 0,
    detail: str = "",
) -> ModelLabWorkUnitReceipt:
    lab_work = lab_work_overlay().lab_work
    assert lab_work is not None
    return ModelLabWorkUnitReceipt(
        work_unit_id="gpc-test0001",
        target_host=SIM_A,
        host=SIM_A,
        repo=lab_work.repo,
        commit_sha=lab_work.commit_sha,
        lane=lab_work.lane,
        kind="other",
        status=status,
        exit_code=exit_code,
        output_tail=tail,
        detail=detail,
    )


def ok_line(host: str = SIM_A) -> str:
    result = ModelGpuPowerCapsResult(
        host=host,
        outcome=EnumGpuPowerCapsOutcome.OK,
        cards=(
            ModelGpuPowerCapsCardResult(
                name="Sim Card Big",
                wanted_watts=500,
                index=0,
                before_watts=600,
                after_watts=500,
                action=EnumGpuPowerCapsCardAction.APPLIED,
            ),
        ),
    )
    return RESULT_LINE_PREFIX + result.model_dump_json()


def ask(
    sender: FakeSender, host: str = SIM_A, requested_by: str = "lane-a"
) -> ModelGpuPowerCapsResult:
    handler = HandlerGpuPowerCaps(overlay=lab_work_overlay(), sender=sender)
    return handler.handle(
        ModelGpuPowerCapsRequest(host=host, requested_by=requested_by)
    )


def test_lab_work_sends_one_unit_to_the_named_host_and_returns_the_hosts_result() -> (
    None
):
    sender = FakeSender(
        receipt(EnumLabWorkUnitStatus.COMPLETED, tail="uv noise\n" + ok_line() + "\n")
    )
    result = ask(sender)
    lab_work = lab_work_overlay().lab_work
    assert lab_work is not None
    assert len(sender.calls) == 1
    unit = sender.calls[0]
    assert unit.target_host == SIM_A
    assert (unit.repo, unit.commit_sha, unit.lane) == (
        lab_work.repo,
        lab_work.commit_sha,
        lab_work.lane,
    )
    assert unit.timeout_seconds == lab_work.unit_timeout_seconds
    assert unit.kind == "other"
    assert not any("{host}" in w or "{requested_by}" in w for w in unit.argv)
    assert SIM_A in unit.argv
    assert "lane-a" in unit.argv
    assert result.outcome is EnumGpuPowerCapsOutcome.OK
    assert result.cards[0].after_watts == 500


def test_lab_work_the_last_result_line_wins_and_noise_lines_are_ignored() -> None:
    tail = (
        RESULT_LINE_PREFIX
        + "{not json\n"
        + ok_line()
        + "\nwarning: something after the result\n"
    )
    result = ask(FakeSender(receipt(EnumLabWorkUnitStatus.COMPLETED, tail=tail)))
    assert result.outcome is EnumGpuPowerCapsOutcome.OK


def test_lab_work_an_unknown_host_is_refused_before_anything_is_sent() -> None:
    sender = FakeSender(receipt(EnumLabWorkUnitStatus.COMPLETED, tail=ok_line()))
    result = ask(sender, host="no-such-host")
    assert result.outcome is EnumGpuPowerCapsOutcome.REFUSED
    assert result.refusal is EnumGpuPowerCapsRefusal.HOST_UNKNOWN
    assert sender.calls == []


@pytest.mark.parametrize(
    "status", [EnumLabWorkUnitStatus.INFRA_ERROR, EnumLabWorkUnitStatus.REFUSED]
)
def test_lab_work_an_unanswered_unit_is_a_typed_transport_refusal(
    status: EnumLabWorkUnitStatus,
) -> None:
    result = ask(FakeSender(receipt(status, exit_code=None, detail="no terminal")))
    assert result.outcome is EnumGpuPowerCapsOutcome.REFUSED
    assert result.refusal is EnumGpuPowerCapsRefusal.TRANSPORT_UNAVAILABLE
    assert result.detail is not None
    assert "no terminal" in result.detail
    assert result.cards == ()


def test_lab_work_a_timed_out_unit_is_failed_and_says_the_limits_are_unknown() -> None:
    result = ask(FakeSender(receipt(EnumLabWorkUnitStatus.TIMED_OUT, exit_code=None)))
    assert result.outcome is EnumGpuPowerCapsOutcome.FAILED
    assert result.detail is not None
    assert "unknown" in result.detail


@pytest.mark.parametrize(
    "tail",
    [
        "no result line here\n",
        RESULT_LINE_PREFIX + "{not json\n",
        ok_line(host="another-host") + "\n",
    ],
)
def test_lab_work_a_completed_unit_without_a_readable_result_is_failed(
    tail: str,
) -> None:
    result = ask(
        FakeSender(receipt(EnumLabWorkUnitStatus.COMPLETED, tail=tail, exit_code=7))
    )
    assert result.outcome is EnumGpuPowerCapsOutcome.FAILED
    assert result.detail is not None
    assert "no readable result" in result.detail
    assert "exit code 7" in result.detail


def test_lab_work_the_overlay_needs_its_block_and_no_transport_prefix() -> None:
    no_block = overlay_document()
    del no_block["lab_work"]
    with pytest.raises(ValidationError, match="needs a lab_work block"):
        ModelGpuPowerCapsOverlay.model_validate(no_block)
    prefixed = overlay_document()
    prefixed["hosts"][0]["transport_prefix"] = ["ssh", "x"]
    with pytest.raises(ValidationError, match="no transport prefix"):
        ModelGpuPowerCapsOverlay.model_validate(prefixed)
    no_word = overlay_document()
    no_word["lab_work"]["host_argv"] = ["uv", "run", "{host}"]
    with pytest.raises(ValidationError, match="requested_by"):
        ModelGpuPowerCapsOverlay.model_validate(no_word)


def test_lab_work_the_shipped_example_overlay_validates() -> None:
    overlay = ModelGpuPowerCapsOverlay.model_validate(
        yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    )
    assert overlay.backend is EnumGpuPowerCapsBackend.LAB_WORK


def test_lab_work_has_no_local_backend() -> None:
    with pytest.raises(ValueError, match="no local backend"):
        select_backend(lab_work_overlay())
