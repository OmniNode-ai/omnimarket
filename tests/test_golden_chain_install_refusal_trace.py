# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for the install refusal trace."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_install_refusal_trace_compute.handlers import (
    HandlerInstallRefusalTraceCompute,
)
from omnimarket.nodes.node_install_refusal_trace_compute.models import (
    ModelInstallRefusalTraceRequest,
    ModelInstallRefusalTraceResult,
    ModelRefusalRecord,
)

pytestmark = pytest.mark.unit

_LINE = (
    "handoff_row: svc-a#12 NOT-READY this PR changes what runs on a host (scripts/x.py) and its proof "
    "is not enough: no Lab line, lab comment or STATUS row carries installed=<host>:<revision>."
)


def test_golden_chain_refusal_records_to_counts_per_cause() -> None:
    result = HandlerInstallRefusalTraceCompute().handle(
        ModelInstallRefusalTraceRequest(
            window_start="2026-11-01T00:00:00Z",
            window_end="2026-11-07T00:00:00Z",
            records=(
                ModelRefusalRecord(source="detail-1", text=_LINE),
                ModelRefusalRecord(source="detail-2", text=_LINE),
            ),
        )
    )
    assert isinstance(result, ModelInstallRefusalTraceResult)
    assert [(c.cause, c.count) for c in result.classes] == [("no-installed-claim", 2)]
    assert result.total == 2
    assert result.install_proof_total == 2
    assert "observation-names-no-run" in result.unread_classes


def test_golden_chain_contract_publishes_the_computed_event() -> None:
    contract = yaml.safe_load(
        (
            Path(__file__).resolve().parents[1]
            / "src/omnimarket/nodes/node_install_refusal_trace_compute/contract.yaml"
        ).read_text(encoding="utf-8")
    )
    computed = "onex.evt.omnimarket.install-refusal-trace-computed.v1"
    assert contract["terminal_event"] == computed
    assert contract["event_bus"]["publish_topics"] == [computed]


def test_error_chain_malformed_request_never_reaches_the_handler() -> None:
    with pytest.raises(ValidationError) as caught:
        ModelInstallRefusalTraceRequest.model_validate({"records": [{"source": 1}]})
    assert {"window_start", "window_end"} <= {
        err["loc"][0] for err in caught.value.errors()
    }


def test_error_chain_hostile_text_is_unclassified_not_raised() -> None:
    hostile = "NOT-READY " * 5000 + "\r\n|"
    result = HandlerInstallRefusalTraceCompute().handle(
        ModelInstallRefusalTraceRequest(
            window_start="a",
            window_end="b",
            records=(ModelRefusalRecord(source="h", text=hostile),),
        )
    )
    assert result.unclassified == ("h",)
    assert result.classes == ()
