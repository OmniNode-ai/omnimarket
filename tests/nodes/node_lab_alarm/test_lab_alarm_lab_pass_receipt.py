# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-pass-receipt condition of node_lab_alarm and its seam to the alarm delivery.

Golden: a passing receipt for the lane's delivered sha reads OK and the event chain is started then
completed; a failing receipt alarms, is delivered once through the delivery double, and the next OK
ends the episode. Error: an unresolved sha, an unreadable surface, an absent or malformed receipt,
a receipt that fails validation, or a receipt for another commit or lane is INDETERMINATE, never
ALARM and never OK; a failed delivery exits as an alarm and names delivery-failed; an unwritable
record exits 4 through the CLI. Failing control: the same failing receipt reads ALARM, and the same
receipt with result PASS and all checks ok reads OK; a receipt for a different sha reads
INDETERMINATE not OK; a drill with PLANTED_FAIL alarms without reading, delivering or recording even
when the sha source is unresolved. Seam: the alarm line a run produces, split at the first ': ',
builds a valid ModelLabAlarm accepted by the real HandlerLabAlarmDeliver with the in-memory backend;
every topic name the handler's events_for emits is declared in the contract's topics and
externally_consumed_topics. The overlay loads without the block, the verb refuses an overlay without
the block, the CLI exit codes follow the outcome, and the HttpDeliveredShaSource reads a local
one-shot http.server answer and refuses non-200, non-JSON, or non-matching-sha bodies.
"""

from __future__ import annotations

import base64
import http.server
import io
import json
import threading
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.scm.handler_gateway_read_client import GatewayReadError
from omnimarket.models.lab_alarm_delivery import ModelLabAlarm
from omnimarket.models.model_github_gateway_io import (
    EnumArtifactFetchRefusal,
    EnumGithubGatewayOperation,
    GithubGatewayResult,
    ModelArtifactContent,
    ModelArtifactsResult,
    ModelArtifactSummary,
    ModelGithubGatewayRequest,
    ModelWorkflowRunsResult,
    ModelWorkflowRunSummary,
)
from omnimarket.nodes.node_lab_alarm.adapters.condition_adapters import (
    InMemoryDeliveredShaSource,
    InMemoryLabPassReceiptSource,
)
from omnimarket.nodes.node_lab_alarm.adapters.inmemory_adapters import (
    InMemoryRunRecords,
)
from omnimarket.nodes.node_lab_alarm.adapters.receipt_adapters import (
    GatewayLabPassReceiptSource,
    HttpDeliveredShaSource,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_lab_pass_receipt import (
    CONDITION,
    HandlerLabPassReceipt,
    events_for,
    load_topics,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_lab_pass_receipt import (
    EnumLabPassReceiptFault,
    EnumLabPassReceiptOutcome,
    ModelLabPassReceiptCommand,
)
from omnimarket.nodes.node_lab_alarm.overlay import (
    ModelLabAlarmOverlay,
    ModelLabPassReceiptSettings,
)
from omnimarket.nodes.node_lab_alarm.protocols import (
    ReceiptAbsentError,
    ReceiptMalformedError,
    ReceiptSurfaceUnreadableError,
    ShaUnresolvedError,
)
from omnimarket.nodes.node_lab_alarm.runtime_lab_alarm_conditions import (
    run_lab_pass_receipt,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.adapters.inmemory_adapters import (
    InMemoryAlarmChannel,
    InMemoryAlarmLedger,
    InMemoryDeliveryState,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.handlers.handler_lab_alarm_deliver import (
    HandlerLabAlarmDeliver,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import ModelDeliveryOverlay

pytestmark = pytest.mark.unit

_NODE = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes/node_lab_alarm"
_DELIVER = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_lab_alarm_deliver_effect"
)
_CLOCK = datetime(2026, 11, 14, 4, 0, tzinfo=UTC)
_SHA = "a" * 40
_OTHER_SHA = "b" * 40
_LANE = "example-receipt-lane"


class _FakeGatewayClient:
    """Serves typed gateway results by operation and run, records every request, or refuses with a typed error."""

    def __init__(
        self,
        runs: ModelWorkflowRunsResult | None = None,
        artifacts: dict[int, ModelArtifactsResult] | None = None,
        fetched: ModelArtifactsResult | None = None,
        error: str | None = None,
    ) -> None:
        self.runs = runs
        self.artifacts = artifacts or {}
        self.fetched = fetched
        self.error = error
        self.requests: list[ModelGithubGatewayRequest] = []

    def read(self, request: ModelGithubGatewayRequest) -> GithubGatewayResult:
        self.requests.append(request)
        if self.error is not None:
            raise GatewayReadError(self.error)
        if request.operation is EnumGithubGatewayOperation.WORKFLOW_RUNS:
            assert self.runs is not None
            return self.runs
        if request.artifact_id is not None:
            assert self.fetched is not None
            return self.fetched
        assert request.run_id is not None
        return self.artifacts[request.run_id]


def _settings() -> ModelLabPassReceiptSettings:
    return ModelLabPassReceiptSettings(
        repository="example-org/example-repo",
        workflow="example-rebuild.yml",
        lane=_LANE,
        agent_url="http://agent.example:8080",
        ready_url="http://ready.example:9090/ready",
        runs_limit=5,
        max_archive_bytes=262144,
        timeout_seconds=10,
    )


def _command(
    *,
    write: bool = True,
    fault: EnumLabPassReceiptFault = EnumLabPassReceiptFault.NONE,
) -> ModelLabPassReceiptCommand:
    return ModelLabPassReceiptCommand(
        correlation_id="receipt-1", write=write, fault=fault
    )


def _pass_receipt(sha: str = _SHA, lane: str = _LANE) -> dict[str, object]:
    return {
        "sha": sha,
        "lane": lane,
        "started_at": "2026-11-14T03:50:00+00:00",
        "finished_at": "2026-11-14T03:55:00+00:00",
        "result": "PASS",
        "checks": [
            {"name": "build", "ok": True, "evidence": "ok"},
            {"name": "test", "ok": True, "evidence": "ok"},
        ],
    }


def _fail_receipt(sha: str = _SHA, lane: str = _LANE) -> dict[str, object]:
    return {
        "sha": sha,
        "lane": lane,
        "started_at": "2026-11-14T03:50:00+00:00",
        "finished_at": "2026-11-14T03:55:00+00:00",
        "result": "FAIL",
        "checks": [
            {"name": "build", "ok": True, "evidence": "ok"},
            {"name": "test", "ok": False, "evidence": "assertion failed"},
        ],
    }


class _Rig:
    def __init__(
        self,
        document: dict[str, object] | None = None,
        *,
        settings: ModelLabPassReceiptSettings | None = None,
        deliver: bool = True,
        sha_error: str | None = None,
        receipt_error: str | None = None,
        receipt_malformed: str | None = None,
        records_unwritable: str | None = None,
    ) -> None:
        self.sha_source = InMemoryDeliveredShaSource(
            sha=_SHA if sha_error is None else None,
            surface="inmemory",
            error=sha_error,
        )
        self.receipts = InMemoryLabPassReceiptSource(
            documents={_SHA: document} if document is not None else {},
            error=receipt_error,
            malformed=receipt_malformed,
        )
        self.records = InMemoryRunRecords()
        if records_unwritable is not None:
            self.records.unwritable = records_unwritable
        self.channel = InMemoryAlarmChannel()
        self.ledger = InMemoryAlarmLedger()
        delivery = HandlerLabAlarmDeliver(
            overlay=ModelDeliveryOverlay.model_validate(
                yaml.safe_load(
                    (_DELIVER / "overlay.example.yaml").read_text(encoding="utf-8")
                )
            ),
            channel=self.channel,
            ledger=self.ledger,
            state=InMemoryDeliveryState(),
            clock=lambda: _CLOCK,
        )
        self.handler = HandlerLabPassReceipt(
            settings=settings if settings is not None else _settings(),
            sha_source=self.sha_source,
            receipts=self.receipts,
            delivery=delivery if deliver else None,
            records=self.records,
            clock=lambda: _CLOCK,
        )


def test_lab_pass_receipt_golden_chain() -> None:
    """A passing receipt reads OK and the event chain is started then completed; a failing receipt
    alarms, delivers once through the delivery double, and the next OK ends the episode."""
    topics = load_topics()
    rig = _Rig(document=_pass_receipt())
    golden = rig.handler.handle(_command())
    assert golden.outcome is EnumLabPassReceiptOutcome.OK
    assert golden.alarms == ()
    assert golden.reasons == ()
    assert golden.failure == ""
    assert "lane example-receipt-lane receipt for" in golden.evidence
    assert "is PASS over 2 checks" in golden.evidence
    assert "surface: inmemory" in golden.evidence
    assert [t for t, _ in events_for(golden, topics)] == [
        topics["lab_pass_receipt_run_started"],
        topics["lab_pass_receipt_run_completed"],
    ]
    rig.receipts.documents = {_SHA: _fail_receipt()}
    breach = rig.handler.handle(_command())
    assert breach.outcome is EnumLabPassReceiptOutcome.ALARM
    [alarm] = breach.alarms
    assert alarm.startswith(
        f"lab-pass-receipt:{_SHA}: compose lane {_LANE} receipt for {_SHA} is FAIL on "
    )
    assert "test" in alarm
    assert "finished 2026-11-14T03:55:00+00:00" in alarm
    assert breach.delivery == (
        f"lab_pass_receipt:lab-pass-receipt:{_SHA} channel=delivered ledger=delivered",
    )
    [(_, text)] = rig.channel.posts
    assert _SHA in text
    [(_, row)] = rig.ledger.rows
    assert f"subject=lab-pass-receipt:{_SHA}" in row.split(" | ")
    rig.receipts.documents = {_SHA: _pass_receipt()}
    ok = rig.handler.handle(_command())
    assert ok.outcome is EnumLabPassReceiptOutcome.OK
    rig.receipts.documents = {_SHA: _fail_receipt()}
    rig.handler.handle(_command())
    assert len(rig.channel.posts) == 2
    assert len(rig.ledger.rows) == 1, "the ledger window still holds the first row"


def test_lab_pass_receipt_error_chain() -> None:
    """Unresolved sha, unreadable surface, absent receipt, malformed receipt, a document failing
    validation, and a receipt for another sha or lane are INDETERMINATE, never ALARM or OK; a failed
    delivery names delivery-failed; an unwritable record exits 4 through the CLI."""
    topics = load_topics()
    rig = _Rig(sha_error="no delivered sha from the declared surfaces")
    unreadable = rig.handler.handle(_command())
    assert unreadable.outcome is EnumLabPassReceiptOutcome.INDETERMINATE
    assert unreadable.reasons == ("sha-unresolved",)
    assert unreadable.alarms == ()
    assert unreadable.failure == "indeterminate:sha-unresolved"
    assert [t for t, _ in events_for(unreadable, topics)][-1] == topics[
        "lab_pass_receipt_run_failed"
    ]
    rig2 = _Rig(document=_pass_receipt(), receipt_error="surface unreachable")
    surface_unreadable = rig2.handler.handle(_command())
    assert surface_unreadable.outcome is EnumLabPassReceiptOutcome.INDETERMINATE
    assert surface_unreadable.reasons == ("receipt-surface-unreadable",)
    rig3 = _Rig(document=None)
    absent = rig3.handler.handle(_command())
    assert absent.outcome is EnumLabPassReceiptOutcome.INDETERMINATE
    assert absent.reasons == ("receipt-absent",)
    rig4 = _Rig(document=_pass_receipt(), receipt_malformed="not a zip")
    malformed = rig4.handler.handle(_command())
    assert malformed.outcome is EnumLabPassReceiptOutcome.INDETERMINATE
    assert malformed.reasons == ("receipt-malformed",)
    rig5 = _Rig(document={"sha": _SHA, "lane": _LANE, "result": "PASS", "checks": []})
    invalid = rig5.handler.handle(_command())
    assert invalid.outcome is EnumLabPassReceiptOutcome.INDETERMINATE
    assert invalid.reasons == ("receipt-malformed",)
    rig6 = _Rig(document=_pass_receipt(sha=_OTHER_SHA))
    stale = rig6.handler.handle(_command())
    assert stale.outcome is EnumLabPassReceiptOutcome.INDETERMINATE
    assert stale.reasons == ("receipt-stale",)
    rig7 = _Rig(document=_fail_receipt(lane="other-lane"))
    wrong_lane = rig7.handler.handle(_command())
    assert wrong_lane.outcome is EnumLabPassReceiptOutcome.INDETERMINATE
    assert wrong_lane.reasons == ("receipt-stale",)
    rig8 = _Rig(document=_fail_receipt())
    rig8.ledger.refuse = "ledger host unreachable"
    failed = rig8.handler.handle(_command())
    assert "delivery-failed" in failed.failure
    assert "ledger host unreachable" in failed.failure
    assert [t for t, _ in events_for(failed, topics)][-1] == topics[
        "lab_pass_receipt_run_failed"
    ]
    rig9 = _Rig(document=_pass_receipt(), records_unwritable="disk full")
    unwritable = rig9.handler.handle(_command())
    assert "record-unwritable" in unwritable.failure
    assert "disk full" in unwritable.failure


def test_lab_pass_receipt_failing_control() -> None:
    """The same failing receipt reads ALARM, and the same receipt with result PASS and all checks ok
    reads OK; a receipt for a different sha reads INDETERMINATE not OK; a drill with PLANTED_FAIL
    alarms without reading, delivering or recording even when the sha source is unresolved."""
    rig = _Rig(document=_fail_receipt())
    assert rig.handler.handle(_command()).outcome is EnumLabPassReceiptOutcome.ALARM
    rig2 = _Rig(document=_pass_receipt())
    assert rig2.handler.handle(_command()).outcome is EnumLabPassReceiptOutcome.OK
    rig3 = _Rig(document=_pass_receipt(sha=_OTHER_SHA))
    assert (
        rig3.handler.handle(_command()).outcome
        is EnumLabPassReceiptOutcome.INDETERMINATE
    )
    rig4 = _Rig(sha_error="no delivered sha")
    drill = rig4.handler.handle(_command(fault=EnumLabPassReceiptFault.PLANTED_FAIL))
    assert drill.outcome is EnumLabPassReceiptOutcome.ALARM
    assert drill.alarms == (
        "lab-pass-receipt:(injected): compose lane example-receipt-lane receipt for (injected) is FAIL on "
        "(injected) (injected: planted-fail)",
    )
    assert drill.delivery == ()
    assert rig4.records.records == []


def test_lab_pass_receipt_seam() -> None:
    """The alarm line a run produces, split at the first ': ', builds a valid ModelLabAlarm accepted
    by the real HandlerLabAlarmDeliver with the in-memory backend; every topic name the handler's
    events_for emits is declared in the contract's topics and externally_consumed_topics."""
    rig = _Rig(document=_fail_receipt())
    result = rig.handler.handle(_command())
    [alarm] = result.alarms
    subject, separator, detail = alarm.partition(": ")
    assert separator == ": "
    assert subject == f"lab-pass-receipt:{_SHA}"
    model = ModelLabAlarm(condition=CONDITION, subject=subject, detail=detail)
    assert model.condition == CONDITION
    assert model.subject == subject
    assert model.detail == detail
    topics = load_topics()
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text(encoding="utf-8"))
    declared = set(contract["topics"])
    external = set(contract.get("externally_consumed_topics", []))
    emitted = {t for t, _ in events_for(result, topics)}
    assert {k for k, v in topics.items() if v in emitted} <= declared
    assert emitted <= set(contract["topics"].values())
    assert emitted <= external, "every emitted topic is declared as externally consumed"


def test_lab_pass_receipt_probe_delivers_and_records_nothing() -> None:
    rig = _Rig(document=_fail_receipt())
    result = rig.handler.handle(_command(write=False))
    assert result.outcome is EnumLabPassReceiptOutcome.ALARM
    assert result.delivery == ()
    assert rig.channel.posts == []
    assert rig.records.records == []


def test_lab_pass_receipt_cli_exit_codes_follow_the_outcome() -> None:
    overlay = ModelLabAlarmOverlay.model_validate(
        yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    )
    rig = _Rig(document=_fail_receipt())
    lines: list[str] = []
    assert (
        run_lab_pass_receipt(
            "lab-pass-receipt-probe",
            EnumLabPassReceiptFault.NONE,
            overlay=overlay,
            handler=rig.handler,
            out=lines.append,
        )
        == 2
    )
    assert any(
        "outcome=ALARM" in line and line.startswith("LAB-PASS-RECEIPT")
        for line in lines
    )
    assert any(line.startswith(f"ALARM lab-pass-receipt:{_SHA}") for line in lines)
    rig.receipts.documents = {_SHA: _pass_receipt()}
    assert (
        run_lab_pass_receipt(
            "lab-pass-receipt",
            EnumLabPassReceiptFault.NONE,
            overlay=overlay,
            handler=rig.handler,
            out=lines.append,
        )
        == 0
    )
    rig.sha_source.error = "no delivered sha"
    assert (
        run_lab_pass_receipt(
            "lab-pass-receipt",
            EnumLabPassReceiptFault.NONE,
            overlay=overlay,
            handler=rig.handler,
            out=lines.append,
        )
        == 3
    )


def test_lab_pass_receipt_verb_refuses_an_overlay_without_the_block(
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    del raw["lab_pass_receipt"]
    code = run_lab_pass_receipt(
        "lab-pass-receipt",
        EnumLabPassReceiptFault.NONE,
        overlay=ModelLabAlarmOverlay.model_validate(raw),
    )
    assert code == 1
    assert "no lab_pass_receipt block" in capsys.readouterr().err


def test_lab_pass_receipt_overlay_loads_without_the_block() -> None:
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    assert ModelLabAlarmOverlay.model_validate(raw).lab_pass_receipt is not None
    del raw["lab_pass_receipt"]
    assert ModelLabAlarmOverlay.model_validate(raw).lab_pass_receipt is None


def _serve(status: int, body: bytes) -> tuple[http.server.HTTPServer, str]:
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return None

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_lab_pass_receipt_http_sha_source_reads_the_agent_health() -> None:
    server, base = _serve(200, json.dumps({"loaded_code_sha": _SHA}).encode("utf-8"))
    try:
        source = HttpDeliveredShaSource(base, None, 5)
        sha, surface = source.read()
        assert sha == _SHA
        assert surface == "deploy-agent loaded_code_sha"
    finally:
        server.shutdown()


def test_lab_pass_receipt_http_sha_source_falls_back_to_ready_revision() -> None:
    server, base = _serve(200, json.dumps({"revision": _SHA}).encode("utf-8"))
    try:
        source = HttpDeliveredShaSource("http://127.0.0.1:9", f"{base}/ready", 5)
        sha, surface = source.read()
        assert sha == _SHA
        assert surface == "runtime /ready revision"
    finally:
        server.shutdown()


def test_lab_pass_receipt_http_sha_source_raises_when_both_silent() -> None:
    source = HttpDeliveredShaSource("http://127.0.0.1:9", None, 2)
    with pytest.raises(ShaUnresolvedError):
        source.read()


def test_lab_pass_receipt_http_sha_source_refuses_non_matching_sha() -> None:
    server, base = _serve(200, json.dumps({"loaded_code_sha": "xyz"}).encode("utf-8"))
    try:
        source = HttpDeliveredShaSource(base, None, 5)
        with pytest.raises(ShaUnresolvedError):
            source.read()
    finally:
        server.shutdown()


def _zip(members: dict[str, bytes]) -> str:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _run(run_id: int) -> ModelWorkflowRunSummary:
    return ModelWorkflowRunSummary(
        id=run_id,
        name="rebuild",
        workflow_id=100,
        head_branch="main",
        head_sha=_SHA,
        event="push",
        status="completed",
        conclusion="success",
        run_number=run_id,
        run_attempt=1,
        created_at="2026-11-14T03:50:00Z",
        updated_at="2026-11-14T03:55:00Z",
        html_url=f"https://example.invalid/runs/{run_id}",
    )


def _artifact(
    artifact_id: int,
    *,
    name: str | None = None,
    created_at: str = "2026-11-14T03:55:00Z",
    expired: bool = False,
) -> ModelArtifactSummary:
    return ModelArtifactSummary(
        id=artifact_id,
        name=name or f"lab-pass-receipt-{_LANE}-{_SHA}",
        size_in_bytes=100,
        expired=expired,
        created_at=created_at,
    )


def _listing(
    run_id: int, artifacts: list[ModelArtifactSummary]
) -> ModelArtifactsResult:
    return ModelArtifactsResult(
        repo="example-org/example-repo",
        run_id=run_id,
        artifacts=artifacts,
        truncated=False,
    )


def _fetch(run_id: int, artifact_id: int, content_base64: str) -> ModelArtifactsResult:
    return ModelArtifactsResult(
        repo="example-org/example-repo",
        run_id=run_id,
        artifacts=[],
        fetched=ModelArtifactContent(
            artifact_id=artifact_id,
            name=f"lab-pass-receipt-{_LANE}-{_SHA}",
            size_bytes=100,
            sha256="0" * 64,
            content_base64=content_base64,
        ),
        truncated=False,
    )


def _runs(*run_ids: int) -> ModelWorkflowRunsResult:
    return ModelWorkflowRunsResult(
        repo="example-org/example-repo",
        runs=[_run(run_id) for run_id in run_ids],
        truncated=False,
    )


def test_lab_pass_receipt_gateway_source_reads_the_newest_receipt() -> None:
    """Two runs at the sha each carry a receipt artifact; the newer one is fetched, with the overlay's bounds."""
    newer = _pass_receipt()
    client = _FakeGatewayClient(
        runs=_runs(1, 2),
        artifacts={
            1: _listing(1, [_artifact(41, created_at="2026-11-14T03:00:00Z")]),
            2: _listing(
                2,
                [
                    _artifact(42, created_at="2026-11-14T03:55:00Z"),
                    _artifact(43, name="another-artifact"),
                    _artifact(44, created_at="2026-11-14T03:59:00Z", expired=True),
                ],
            ),
        },
        fetched=_fetch(
            2, 42, _zip({"lab-pass-receipt/receipt.json": json.dumps(newer).encode()})
        ),
    )
    document = GatewayLabPassReceiptSource(_settings(), client=client).read(_SHA)
    assert document == newer
    runs_request, *_, fetch_request = client.requests
    assert runs_request.operation is EnumGithubGatewayOperation.WORKFLOW_RUNS
    assert runs_request.repo == "example-org/example-repo"
    assert runs_request.workflow == "example-rebuild.yml"
    assert runs_request.head_sha == _SHA
    assert runs_request.limit == 5
    assert fetch_request.operation is EnumGithubGatewayOperation.ARTIFACTS
    assert (fetch_request.run_id, fetch_request.artifact_id) == (2, 42)
    assert fetch_request.max_bytes == 262144


def test_lab_pass_receipt_gateway_source_absent_without_an_unexpired_artifact() -> None:
    no_runs = _FakeGatewayClient(runs=_runs())
    with pytest.raises(ReceiptAbsentError):
        GatewayLabPassReceiptSource(_settings(), client=no_runs).read(_SHA)
    expired_only = _FakeGatewayClient(
        runs=_runs(1), artifacts={1: _listing(1, [_artifact(41, expired=True)])}
    )
    with pytest.raises(ReceiptAbsentError):
        GatewayLabPassReceiptSource(_settings(), client=expired_only).read(_SHA)


@pytest.mark.parametrize(
    "content",
    [
        base64.b64encode(b"not a zip").decode("ascii"),
        _zip({"a/receipt.json": b"{}", "b/receipt.json": b"{}"}),
        _zip({"notes.txt": b"no receipt here"}),
        _zip({"receipt.json": b"not json"}),
        _zip({"receipt.json": b"[1, 2]"}),
        "!!! not base64 !!!",
    ],
    ids=[
        "not-zip",
        "two-receipts",
        "no-receipt",
        "not-json",
        "not-object",
        "not-base64",
    ],
)
def test_lab_pass_receipt_gateway_source_malformed_archive_is_refused(
    content: str,
) -> None:
    client = _FakeGatewayClient(
        runs=_runs(1),
        artifacts={1: _listing(1, [_artifact(41)])},
        fetched=_fetch(1, 41, content),
    )
    with pytest.raises(ReceiptMalformedError):
        GatewayLabPassReceiptSource(_settings(), client=client).read(_SHA)


def test_lab_pass_receipt_gateway_source_malformed_when_the_fetch_is_refused() -> None:
    refused = ModelArtifactsResult(
        repo="example-org/example-repo",
        run_id=1,
        artifacts=[],
        fetch_refused=EnumArtifactFetchRefusal.OVER_SIZE_CAP,
        truncated=False,
    )
    client = _FakeGatewayClient(
        runs=_runs(1), artifacts={1: _listing(1, [_artifact(41)])}, fetched=refused
    )
    with pytest.raises(ReceiptMalformedError):
        GatewayLabPassReceiptSource(_settings(), client=client).read(_SHA)


def test_lab_pass_receipt_gateway_source_unreadable_when_the_gateway_fails() -> None:
    client = _FakeGatewayClient(error="gateway unreachable")
    with pytest.raises(ReceiptSurfaceUnreadableError):
        GatewayLabPassReceiptSource(_settings(), client=client).read(_SHA)


def test_lab_pass_receipt_overlay_refuses_an_archive_bound_past_the_gateways_cap() -> (
    None
):
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    raw["lab_pass_receipt"]["max_archive_bytes"] = 524289
    with pytest.raises(ValueError, match="max_archive_bytes"):
        ModelLabAlarmOverlay.model_validate(raw)
