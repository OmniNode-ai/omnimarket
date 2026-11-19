# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The loaded-code-sha read of node_lab_alarm: the sha the lane's deploy agent has loaded.

Golden: a surface that answers a matching sha reads READ with that sha and the surface that
answered, and the event chain is started then completed; the CLI verb prints LOADED-CODE-SHA
and exits 0. Error: a source that raises ShaUnresolvedError reads UNREAD with the error text,
the chain is started then failed and the CLI exits 3; a result model with outcome READ and no
sha is refused by validation. Failing control: the HttpDeliveredShaSource reads a local
one-shot http.server's health body with loaded_code_sha as READ with the surface
"deploy-agent loaded_code_sha", and the same server answering a non-matching sha reads
UNREAD, never READ. Parity: the lab-pass-receipt condition reading through this operation
still answers INDETERMINATE with reason sha-unresolved when no surface delivers a sha. Seam:
every topic the handler's events_for emits is declared in the contract's topics and
externally_consumed_topics, and the contract's cli_handlers lists loaded_code_sha.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_lab_alarm.adapters.condition_adapters import (
    InMemoryDeliveredShaSource,
    InMemoryLabPassReceiptSource,
)
from omnimarket.nodes.node_lab_alarm.adapters.receipt_adapters import (
    HttpDeliveredShaSource,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_lab_pass_receipt import (
    HandlerLabPassReceipt,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_loaded_code_sha import (
    HandlerLoadedCodeSha,
    events_for,
    load_topics,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_lab_pass_receipt import (
    EnumLabPassReceiptOutcome,
    ModelLabPassReceiptCommand,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_loaded_code_sha import (
    EnumLoadedCodeShaOutcome,
    ModelLoadedCodeShaRequest,
    ModelLoadedCodeShaResult,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelLabAlarmOverlay
from omnimarket.nodes.node_lab_alarm.runtime_lab_alarm_conditions import (
    run_loaded_code_sha,
)
from tests.nodes.node_lab_alarm.test_lab_alarm_lab_pass_receipt import (
    _serve,
    _settings,
)

pytestmark = pytest.mark.unit

_NODE = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes/node_lab_alarm"
_SHA = "a" * 40


def test_loaded_code_sha_golden_read() -> None:
    """A surface that answers a matching sha reads READ with that sha and the surface that
    answered, and the event chain is started then completed; the CLI verb prints
    LOADED-CODE-SHA and exits 0."""
    topics = load_topics()
    handler = HandlerLoadedCodeSha(
        sha_source=InMemoryDeliveredShaSource(sha=_SHA, surface="inmemory")
    )
    result = handler.handle(ModelLoadedCodeShaRequest(correlation_id="sha-1"))
    assert result.outcome is EnumLoadedCodeShaOutcome.READ
    assert result.sha == _SHA
    assert result.surface == "inmemory"
    assert result.error == ""
    assert [t for t, _ in events_for(result, topics)] == [
        topics["loaded_code_sha_run_started"],
        topics["loaded_code_sha_run_completed"],
    ]
    overlay = ModelLabAlarmOverlay.model_validate(
        yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    )
    lines: list[str] = []
    assert run_loaded_code_sha(overlay=overlay, handler=handler, out=lines.append) == 0
    assert any(line.startswith("EVENT topic=") for line in lines)
    assert any(
        line.startswith("LOADED-CODE-SHA")
        and f"sha={_SHA}" in line
        and "surface=inmemory" in line
        and "outcome=READ" in line
        for line in lines
    )


def test_loaded_code_sha_unread_names_the_reason() -> None:
    """A source that raises ShaUnresolvedError reads UNREAD with the error text, the chain is
    started then failed and the CLI exits 3; a result model with outcome READ and no sha is
    refused by validation."""
    topics = load_topics()
    handler = HandlerLoadedCodeSha(
        sha_source=InMemoryDeliveredShaSource(
            error="no delivered sha from the declared surfaces"
        )
    )
    result = handler.handle(ModelLoadedCodeShaRequest(correlation_id="sha-2"))
    assert result.outcome is EnumLoadedCodeShaOutcome.UNREAD
    assert result.sha is None
    assert result.surface is None
    assert result.error == "no delivered sha from the declared surfaces"
    assert [t for t, _ in events_for(result, topics)] == [
        topics["loaded_code_sha_run_started"],
        topics["loaded_code_sha_run_failed"],
    ]
    overlay = ModelLabAlarmOverlay.model_validate(
        yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    )
    lines: list[str] = []
    assert run_loaded_code_sha(overlay=overlay, handler=handler, out=lines.append) == 3
    assert any(
        line.startswith("LOADED-CODE-SHA")
        and "sha=none" in line
        and "surface=none" in line
        and "outcome=UNREAD" in line
        for line in lines
    )
    with pytest.raises(ValidationError):
        ModelLoadedCodeShaResult(
            correlation_id="sha-3", outcome=EnumLoadedCodeShaOutcome.READ
        )


def test_loaded_code_sha_verb_refuses_an_overlay_without_the_block(
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    del raw["lab_pass_receipt"]
    code = run_loaded_code_sha(overlay=ModelLabAlarmOverlay.model_validate(raw))
    assert code == 1
    assert "no lab_pass_receipt block" in capsys.readouterr().err


def test_loaded_code_sha_http_reads_the_agent_health() -> None:
    """The HttpDeliveredShaSource answers READ from a health body carrying loaded_code_sha,
    with the surface that answered."""
    server, base = _serve(200, json.dumps({"loaded_code_sha": _SHA}).encode("utf-8"))
    try:
        handler = HandlerLoadedCodeSha(sha_source=HttpDeliveredShaSource(base, None, 5))
        result = handler.handle(ModelLoadedCodeShaRequest(correlation_id="http-1"))
        assert result.outcome is EnumLoadedCodeShaOutcome.READ
        assert result.sha == _SHA
        assert result.surface == "deploy-agent loaded_code_sha"
    finally:
        server.shutdown()


def test_loaded_code_sha_http_non_matching_sha_is_unread_never_read() -> None:
    """The same server answering a body whose sha does not match the pattern reads UNREAD,
    never READ."""
    server, base = _serve(200, json.dumps({"loaded_code_sha": "xyz"}).encode("utf-8"))
    try:
        handler = HandlerLoadedCodeSha(sha_source=HttpDeliveredShaSource(base, None, 5))
        result = handler.handle(ModelLoadedCodeShaRequest(correlation_id="http-2"))
        assert result.outcome is EnumLoadedCodeShaOutcome.UNREAD
        assert result.sha is None
    finally:
        server.shutdown()


def test_loaded_code_sha_lab_pass_receipt_parity() -> None:
    """The lab-pass-receipt condition reads its sha through this typed read; with no surface
    delivering one it still answers INDETERMINATE with reason sha-unresolved, as it did
    before the read moved behind the operation."""
    handler = HandlerLabPassReceipt(
        settings=_settings(),
        sha_source=InMemoryDeliveredShaSource(
            error="no delivered sha from the declared surfaces"
        ),
        receipts=InMemoryLabPassReceiptSource(documents={}),
    )
    result = handler.handle(ModelLabPassReceiptCommand(correlation_id="parity-1"))
    assert result.outcome is EnumLabPassReceiptOutcome.INDETERMINATE
    assert result.reasons == ("sha-unresolved",)
    assert result.failure == "indeterminate:sha-unresolved"
    assert result.sha == "(unresolved)"
    assert result.surface == "none"
    assert result.evidence == (
        "the lane's delivered sha could not be resolved, so no receipt could be looked "
        "up; an unresolved sha is not a passing lane: no delivered sha from the declared "
        "surfaces"
    )


def test_loaded_code_sha_seam() -> None:
    """Every topic name the handler's events_for emits is declared in the contract's topics
    and externally_consumed_topics, and the contract's cli_handlers lists loaded_code_sha."""
    handler = HandlerLoadedCodeSha(
        sha_source=InMemoryDeliveredShaSource(sha=_SHA, surface="inmemory")
    )
    result = handler.handle(ModelLoadedCodeShaRequest(correlation_id="seam-1"))
    topics = load_topics()
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text(encoding="utf-8"))
    emitted = {t for t, _ in events_for(result, topics)}
    assert emitted <= set(contract["topics"].values())
    assert emitted <= set(contract["externally_consumed_topics"])
    assert "loaded_code_sha" in {
        entry["operation"] for entry in contract["cli_handlers"]
    }


def test_loaded_code_sha_result_models_refuse_a_verdict_without_its_evidence() -> None:
    with pytest.raises(ValidationError):
        ModelLoadedCodeShaResult(
            correlation_id="m-1", outcome=EnumLoadedCodeShaOutcome.READ
        )
    with pytest.raises(ValidationError):
        ModelLoadedCodeShaResult(
            correlation_id="m-2", outcome=EnumLoadedCodeShaOutcome.UNREAD
        )
    with pytest.raises(ValidationError):
        ModelLoadedCodeShaResult(
            correlation_id="m-3",
            outcome=EnumLoadedCodeShaOutcome.UNREAD,
            sha=_SHA,
            surface="inmemory",
            error="x",
        )


def test_lab_pass_receipt_reads_its_sha_through_the_typed_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Moved, not copied: the condition's sha read is a call of HandlerLoadedCodeSha.handle
    with the run's correlation id. Failing control: a condition that calls its source directly
    makes no such call and this test is red."""
    seen: list[str] = []
    real = HandlerLoadedCodeSha.handle

    def recording(
        self: HandlerLoadedCodeSha, request: ModelLoadedCodeShaRequest
    ) -> ModelLoadedCodeShaResult:
        seen.append(request.correlation_id)
        return real(self, request)

    monkeypatch.setattr(HandlerLoadedCodeSha, "handle", recording)
    handler = HandlerLabPassReceipt(
        settings=_settings(),
        sha_source=InMemoryDeliveredShaSource(error="none delivered"),
        receipts=InMemoryLabPassReceiptSource(documents={}),
    )
    handler.handle(ModelLabPassReceiptCommand(correlation_id="typed-1"))
    assert seen == ["typed-1"]
