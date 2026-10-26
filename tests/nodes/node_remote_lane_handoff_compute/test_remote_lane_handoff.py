# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLAIM mirroring and delayed no-claim handoffs (OMN-20864)."""

from __future__ import annotations

import importlib
import json
from importlib.resources import files
from pathlib import Path
from typing import Any, get_type_hints

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from pydantic import ValidationError

from omnimarket.nodes.node_remote_lane_handoff_compute import (
    HandlerRemoteLaneHandoff,
    NodeRemoteLaneHandoffCompute,
)
from omnimarket.nodes.node_remote_lane_handoff_compute.models import (
    ModelRemoteLaneHandoffRequest,
    ModelRemoteLaneHandoffResult,
    ModelRemoteLaneOutboxRequest,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

PACKAGE = "omnimarket.nodes.node_remote_lane_handoff_compute"
LANE = "hr-runner-port-0740"
STAMP = "2026-10-25T07:20:23Z"
REQUEST_ID = "2f5dedf8-949e-4b9a-b9a7-8b102b197680"
ROW = (
    f"{STAMP} | CLAIM | lane={LANE} | ticket=OMN-17292 | host=h101 | "
    f"req={REQUEST_ID} | via=bus:h200 | remote lane"
)
CLAIM_OUTPUT = (
    f"OK CLAIM {STAMP} lane={LANE} ticket=OMN-17292 line=0 via=bus "
    f"req={REQUEST_ID} status=pending queued=host-bus-outbox detail=/x/row.txt"
)


def _claim(**updates: Any) -> ModelRemoteLaneHandoffResult:
    fields: dict[str, Any] = {
        "decision": "claim_mirror",
        "lane": LANE,
        "claim_stamp": STAMP,
        "claim_output": CLAIM_OUTPUT,
        "ledger_rows": (f"{STAMP} | STATUS | lane={LANE} | starting",),
        "outbox_requests": (
            ModelRemoteLaneOutboxRequest(request_id=REQUEST_ID, payload=ROW),
        ),
    }
    fields.update(updates)
    return HandlerRemoteLaneHandoff().handle(ModelRemoteLaneHandoffRequest(**fields))


def test_claim_mirror_incident_replay_from_host_bus_outbox() -> None:
    result = _claim()
    assert result.decision == "claim_mirror"
    assert result.claim_row == ROW
    assert result.claim_source == "host-bus-outbox"
    assert result.start_lane is True
    assert result.handoff_status is None
    assert "host-bus-outbox" in result.reason


def test_claim_mirror_ledger_takes_precedence_and_last_match() -> None:
    latest = ROW + " updated"
    result = _claim(ledger_rows=(ROW, "unrelated", latest + "\r\n", "later status"))
    assert result.claim_row == latest
    assert result.claim_source == "ledger"
    assert result.start_lane is True
    assert "ledger" in result.reason


@pytest.mark.parametrize("suffix", ["", "\n", "\r\n", "\r"])
def test_claim_mirror_accepts_lane_as_last_cell(suffix: str) -> None:
    row = f"{STAMP} | CLAIM | ticket=OMN-17292 | lane={LANE}"
    result = _claim(ledger_rows=(row + suffix,))
    assert result.claim_row == row
    assert result.claim_source == "ledger"


@pytest.mark.parametrize(
    "output",
    [f"req={REQUEST_ID} status=pending", f"req={REQUEST_ID} queued=host-bus-outbox"],
)
def test_claim_mirror_either_queue_indicator_allows_outbox(output: str) -> None:
    assert _claim(claim_output=output).claim_row == ROW


def test_claim_mirror_only_reads_the_append_request_id() -> None:
    result = _claim(
        outbox_requests=(
            ModelRemoteLaneOutboxRequest(request_id="another-id", payload=ROW),
        )
    )
    assert result.claim_row is None
    assert result.claim_source is None
    assert result.start_lane is False
    assert f"queued request {REQUEST_ID} not in the outbox" in result.reason


@pytest.mark.parametrize(
    "output", ["", f"req={REQUEST_ID} status=ok", "status=pending"]
)
def test_claim_mirror_no_queued_request_id_does_not_start(output: str) -> None:
    result = _claim(claim_output=output)
    assert result.start_lane is False
    assert result.claim_row is None
    assert result.claim_source is None
    assert "not in the ledger tail and no queued request id" in result.reason


@pytest.mark.parametrize("stamp", [None, ""])
def test_claim_mirror_missing_stamp_does_not_start(stamp: str | None) -> None:
    result = _claim(claim_stamp=stamp, ledger_rows=(ROW,))
    assert result.start_lane is False
    assert "no claim_stamp" in result.reason


@pytest.mark.parametrize(
    "row",
    [
        ROW.replace(LANE, "another-lane"),
        ROW.replace(LANE, LANE + "-suffix"),
        ROW.replace(STAMP, "2026-10-25T07:20:24Z"),
        ROW.replace(" | CLAIM | ", " | STATUS | "),
        "prefix " + ROW,
        ROW + "\rhidden",
        ROW + "\nhidden",
        "hidden\n" + ROW,
    ],
)
def test_claim_mirror_rejects_nonmatching_or_embedded_newline_ledger_rows(
    row: str,
) -> None:
    result = _claim(ledger_rows=(row,), outbox_requests=())
    assert result.start_lane is False
    assert result.claim_row is None


@pytest.mark.parametrize(
    "payload", ["", ROW.replace(LANE, "another-lane"), ROW + "\rhidden"]
)
def test_claim_mirror_request_without_matching_claim_does_not_start(
    payload: str,
) -> None:
    result = _claim(
        outbox_requests=(
            ModelRemoteLaneOutboxRequest(request_id=REQUEST_ID, payload=payload),
        )
    )
    assert result.start_lane is False
    assert result.claim_source is None
    assert f"request {REQUEST_ID} holds no CLAIM of {LANE} at {STAMP}" in result.reason


def test_claim_mirror_matches_payload_lines_without_changing_stamped_cells() -> None:
    result = _claim(
        outbox_requests=(
            ModelRemoteLaneOutboxRequest(request_id="another-id", payload=ROW),
            ModelRemoteLaneOutboxRequest(
                request_id=REQUEST_ID,
                payload=f"{ROW.replace(LANE, 'another-lane')}\r\n{ROW}\r\n",
            ),
        )
    )
    assert result.claim_row == ROW
    assert result.start_lane is True


@pytest.mark.parametrize(
    ("exit_code", "final", "status"),
    [
        (0, False, "handed-off"),
        (0, True, "handed-off"),
        (4, False, "pending"),
        (5, False, "pending"),
        (4, True, "refused"),
        (5, True, "refused"),
        (3, False, "refused"),
        (65, False, "refused"),
        (None, False, "refused"),
    ],
)
def test_handoff_retry_exit_codes(
    exit_code: int | None, final: bool, status: str
) -> None:
    result = HandlerRemoteLaneHandoff().handle(
        ModelRemoteLaneHandoffRequest(
            decision="handoff_retry",
            lane=LANE,
            handoff_exit=exit_code,
            handoff_output="other refusal",
            final_attempt=final,
        )
    )
    assert result.decision == "handoff_retry"
    assert result.handoff_status == status
    assert f"exit {exit_code}" in result.reason
    assert result.start_lane is False
    assert result.claim_row is None
    assert result.claim_source is None


@pytest.mark.parametrize(
    ("on_ledger", "final", "stamp", "status"),
    [
        (False, False, STAMP, "pending"),
        (False, True, STAMP, "refused"),
        (True, False, STAMP, "refused"),
        (True, True, STAMP, "refused"),
        (False, False, None, "refused"),
        (False, False, "", "refused"),
    ],
)
def test_handoff_retry_no_claim(
    on_ledger: bool, final: bool, stamp: str | None, status: str
) -> None:
    result = HandlerRemoteLaneHandoff().handle(
        ModelRemoteLaneHandoffRequest(
            decision="handoff_retry",
            lane=LANE,
            claim_stamp=stamp,
            handoff_exit=3,
            handoff_output=f"REFUSED 3 no-claim: {LANE} has no CLAIM",
            claim_on_ledger=on_ledger,
            final_attempt=final,
        )
    )
    assert result.handoff_status == status
    assert "exit 3" in result.reason
    if stamp and not on_ledger:
        assert f"CLAIM at {STAMP} had not reached the live ledger" in result.reason
    if on_ledger:
        assert "on the live ledger" in result.reason


def test_models_are_frozen_and_forbid_extra_fields() -> None:
    models = (
        ModelRemoteLaneOutboxRequest(request_id=REQUEST_ID, payload=ROW),
        ModelRemoteLaneHandoffRequest(decision="claim_mirror", lane=LANE),
        ModelRemoteLaneHandoffResult(decision="claim_mirror", reason="no claim_stamp"),
    )
    for model in models:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            type(model).model_validate({**model.model_dump(), "unknown": True})
        field = next(iter(type(model).model_fields))
        with pytest.raises(ValidationError, match="frozen_instance"):
            setattr(model, field, "changed")
    with pytest.raises(ValidationError):
        ModelRemoteLaneHandoffRequest(decision="claim_mirror", lane="")
    with pytest.raises(ValidationError):
        ModelRemoteLaneHandoffRequest.model_validate(
            {"decision": "unknown", "lane": LANE}
        )


def test_contract_golden_chain() -> None:
    contract = yaml.safe_load(files(PACKAGE).joinpath("contract.yaml").read_text())
    assert contract["name"] == "node_remote_lane_handoff_compute"
    assert contract["metadata"]["ticket"] == "OMN-20864"
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["descriptor"]["side_effects"] == []
    assert contract["event_bus"] == {
        "subscribe_topics": ["onex.cmd.omnimarket.remote-lane-handoff-requested.v1"],
        "publish_topics": ["onex.evt.omnimarket.remote-lane-handoff-decided.v1"],
    }
    assert (
        contract["runtime_dispatch"]["command_topic"]
        == contract["event_bus"]["subscribe_topics"][0]
    )
    assert contract["terminal_event"] == contract["event_bus"]["publish_topics"][0]
    assert (
        contract["externally_consumed_topics"]
        == contract["event_bus"]["publish_topics"]
    )
    (entry,) = contract["handler_routing"]["handlers"]
    assert entry["operation"] == "decide_remote_lane_handoff"
    handler_type = getattr(
        importlib.import_module(entry["handler"]["module"]), entry["handler"]["name"]
    )
    assert handler_type is HandlerRemoteLaneHandoff
    for key, expected in (
        ("input_model", ModelRemoteLaneHandoffRequest),
        ("output_model", ModelRemoteLaneHandoffResult),
    ):
        module, _, name = entry[key].rpartition(".")
        assert getattr(importlib.import_module(module), name) is expected
    hints = get_type_hints(handler_type.handle)
    assert hints["request"] is ModelRemoteLaneHandoffRequest
    assert hints["return"] is ModelRemoteLaneHandoffResult
    request = ModelRemoteLaneHandoffRequest(
        decision="claim_mirror", lane=LANE, claim_stamp=STAMP, ledger_rows=(ROW,)
    )
    result = handler_type().handle(request)
    assert isinstance(result, ModelRemoteLaneHandoffResult)
    assert result.claim_row == ROW
    assert (
        NodeRemoteLaneHandoffCompute.contract_path.read_text()
        == files(PACKAGE).joinpath("contract.yaml").read_text()
    )
    assert NodeRemoteLaneHandoffCompute().handle(request) == result


@pytest.mark.parametrize("decision", ["claim_mirror", "handoff_retry"])
def test_contract_runtime_answers_on_terminal_topic(
    tmp_path: Path, decision: str
) -> None:
    payload = {
        "decision": decision,
        "lane": LANE,
        "claim_stamp": STAMP,
        "claim_output": CLAIM_OUTPUT,
        "outbox_requests": [{"request_id": REQUEST_ID, "payload": ROW}],
        "handoff_exit": 3,
        "handoff_output": "REFUSED 3 no-claim",
    }
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(payload))
    state = tmp_path / "state"
    runtime = RuntimeLocal(
        workflow_path=NodeRemoteLaneHandoffCompute.contract_path,
        state_root=state,
        input_path=input_path,
        timeout=30,
    )
    assert runtime.run() == EnumWorkflowResult.COMPLETED
    result = json.loads((state / "workflow_result.json").read_text())[
        "terminal_payload"
    ]
    assert result["decision"] == decision
    if decision == "claim_mirror":
        assert result["claim_row"] == ROW
        assert result["claim_source"] == "host-bus-outbox"
        assert result["start_lane"] is True
    else:
        assert result["handoff_status"] == "pending"
