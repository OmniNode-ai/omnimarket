# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J10 (piece 10 to piece 11): a runtime-affecting merge yields a PASS lab receipt keyed by its sha, and the running
runtime reads back that exact sha and answers one correlated functional request.

Rebuild plan 4.1, J10 (OMN-22957). Piece 10 (install and release) delivers a merged head; piece 11 (dev deploy and
runtime) proves it ran. The rig runs the join handler against the receipt reader and the runtime probe it is given.
The golden cases bind the real HTTP backends to a runtime served on a loopback socket by the test and to a lane-health
projection document the test serves; the in-memory backends carry the cases that need a shaped answer. Nothing here
touches a real host, the ledger or GitHub. The join is judged the way the floor judges it: piece 10's chain and piece
11's chain share one correlation id and the floor's own receipt fold reads the pair.

Run the two cases with ``-k golden`` and ``-k error``.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from omnimarket.handlers.emission_sink import ChainEmitter, InMemoryEmissionSink
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelJoinDef,
    ModelNodeEmission,
    ModelPieceDef,
)
from omnimarket.nodes.node_dev_deploy_reading_compute.adapters.adapter_in_memory_dev_deploy import (
    InMemoryLabPassReceipts,
    InMemoryRuntime,
)
from omnimarket.nodes.node_dev_deploy_reading_compute.adapters.adapter_real_dev_deploy import (
    HttpRuntimeProbe,
    LaneHealthLabPassReceipts,
)
from omnimarket.nodes.node_dev_deploy_reading_compute.handlers.handler_dev_deploy_join_j10 import (
    HandlerDevDeployJoinJ10,
    load_join_j10_policy,
)
from omnimarket.nodes.node_dev_deploy_reading_compute.models.model_dev_deploy_reading import (
    ModelLabPassReceiptFact,
)
from omnimarket.nodes.node_dev_deploy_reading_compute.models.model_join_j10 import (
    EnumJoinJ10Outcome,
    ModelDevDeployJoinOverlay,
    ModelJoinJ10Request,
    ModelJoinJ10Result,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)

pytestmark = pytest.mark.integration

POLICY = load_join_j10_policy()
MERGED_AT = datetime(2026, 11, 9, 9, 0, 0, tzinfo=UTC)
DEADLINE_S = 3600
HOUR = timedelta(seconds=DEADLINE_S)
LANE = "fixture-lane"
MERGED = "a" * 40
PARENT = "b" * 40
CHAIN_DEADLINE_S = 900


class _Clock:
    """A clock that moves one second per read, so a chain's events keep their order."""

    def __init__(self, start: datetime) -> None:
        self._t = start

    def __call__(self) -> datetime:
        self._t += timedelta(seconds=1)
        return self._t


def _receipt(
    sha: str = MERGED,
    *,
    result: str = "PASS",
    minutes: float = 20,
    lane: str = LANE,
) -> ModelLabPassReceiptFact:
    return ModelLabPassReceiptFact.model_validate(
        {
            "sha": sha,
            "lane": lane,
            "result": result,
            "finished_at": MERGED_AT + timedelta(minutes=minutes),
        }
    )


def _request(
    cid: str, judged_at: datetime, *, runtime_affecting: bool = True
) -> ModelJoinJ10Request:
    return ModelJoinJ10Request(
        correlation_id=cid,
        merged_sha=MERGED,
        merged_at=MERGED_AT,
        judged_at=judged_at,
        runtime_affecting=runtime_affecting,
        required_lane=LANE,
        deadline_seconds=DEADLINE_S,
    )


class _Rig:
    def __init__(self) -> None:
        self.sink = InMemoryEmissionSink()

    def piece10_delivered(self, cid: str) -> None:
        """Piece 10 as the install node answers: a start, then a success carrying the delivery."""
        chain = ChainEmitter(
            self.sink,
            node="node_install_canary_effect",
            piece=POLICY.from_piece,
            clock=_Clock(MERGED_AT),
            correlation_id=cid,
            backend="in_memory",
        )
        chain.start()
        chain.success({"hosts_installed": 1})

    def join(
        self, receipts: Any, runtime: Any, judged_at: datetime
    ) -> HandlerDevDeployJoinJ10:
        return HandlerDevDeployJoinJ10(
            receipts=receipts,
            runtime=runtime,
            sink=self.sink,
            policy=POLICY,
            clock=_Clock(judged_at),
            backend="in_memory",
        )

    def piece11(self) -> list[ModelNodeEmission]:
        return [e for e in self.sink.events if e.piece == POLICY.to_piece]


def _fold(emissions: list[ModelNodeEmission], now: datetime) -> tuple[str, list[str]]:
    receipts = HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=now,
            emissions=tuple(emissions),
            pieces=tuple(
                ModelPieceDef(piece=p, name=f"piece {p}", reading_max_age_s=86400)
                for p in (POLICY.from_piece, POLICY.to_piece)
            ),
            joins=(
                ModelJoinDef(
                    id=POLICY.join_id,
                    from_piece=POLICY.from_piece,
                    to_piece=POLICY.to_piece,
                    label="install and release to dev deploy and runtime",
                ),
            ),
            freshness_max_age_s=86400,
            error_max_age_s=86400,
            chain_deadline_s=CHAIN_DEADLINE_S,
        )
    )
    join = next(j for j in receipts.joins if j.join == POLICY.join_id)
    return join.state, [f.code for f in receipts.findings]


def _ok_receipts() -> InMemoryLabPassReceipts:
    store = InMemoryLabPassReceipts()
    store.add(_receipt())
    return store


@contextmanager
def _serve(
    sha: str | None, *, status: int = 200, echo: bool = True, ok: bool = True
) -> Iterator[str]:
    """A runtime on a loopback socket: it answers with ``sha``, echoes the correlation header and reports ``ok``."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body: dict[str, Any] = {"git_sha": sha, "functional": ok}
            if echo:
                body["correlation_id"] = self.headers.get("X-Correlation-Id")
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/runtime"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _overlay(
    runtime_url: str, receipts_url: str = "http://127.0.0.1:1/unused"
) -> ModelDevDeployJoinOverlay:
    return ModelDevDeployJoinOverlay(
        receipts_url=receipts_url,
        receipt_lane=LANE,
        receipt_pass_word="PASS",
        runtime_url=runtime_url,
        runtime_correlation_header="X-Correlation-Id",
        runtime_sha_key="git_sha",
        runtime_correlation_key="correlation_id",
        runtime_ok_key="functional",
        timeout_seconds=5,
        backend="real",
    )


def _projection(rows: list[dict[str, str]]) -> Any:
    def fetch(url: str, headers: dict[str, str], timeout: int) -> str:
        return json.dumps({"rows": rows})

    return fetch


def test_policy_reads_the_pieces_and_codes_from_the_contract() -> None:
    assert POLICY.join_id == "J10"
    assert (POLICY.from_piece, POLICY.to_piece) == (10, 11)
    assert (
        len(
            {
                POLICY.receipt_missing_code,
                POLICY.receipt_late_code,
                POLICY.receipt_failed_code,
                POLICY.receipts_unreadable_code,
                POLICY.runtime_stale_code,
                POLICY.runtime_broken_code,
                POLICY.runtime_uncorrelated_code,
            }
        )
        == 7
    ), "every cause has its own typed code"


# ---------------------------------------------------------------------------------------------- golden


def test_golden_pass_receipt_and_runtime_reads_back_the_sha_and_answers_the_correlated_request() -> (
    None
):
    rig = _Rig()
    cid = "j10-golden"
    rig.piece10_delivered(cid)
    runtime = InMemoryRuntime(MERGED)
    at = MERGED_AT + timedelta(minutes=30)
    result = rig.join(_ok_receipts(), runtime, at).handle(_request(cid, at))
    assert result.outcome is EnumJoinJ10Outcome.JOINED
    assert result.failure_code is None
    assert result.runtime_observed_sha == MERGED
    assert [(a.correlation_id, a.expected_sha) for a in runtime.asked] == [
        (cid, MERGED)
    ]
    piece11 = rig.piece11()
    assert [e.phase for e in piece11] == ["start", "success"]
    state, findings = _fold(rig.sink.events, at + timedelta(minutes=5))
    assert state == "golden_only", findings
    assert findings == []


def test_golden_real_backends_a_served_runtime_and_the_lane_health_projection() -> None:
    rig = _Rig()
    cid = "j10-golden-real"
    rig.piece10_delivered(cid)
    rows = [
        {
            "lane": LANE,
            "receipt_sha": MERGED,
            "receipt_result": "PASS",
            "receipt_observed_at": (MERGED_AT + timedelta(minutes=20)).isoformat(),
        },
        {
            "lane": LANE,
            "receipt_sha": PARENT,
            "receipt_result": "PASS",
            "receipt_observed_at": (MERGED_AT - timedelta(hours=3)).isoformat(),
        },
    ]
    at = MERGED_AT + timedelta(minutes=30)
    with _serve(MERGED) as url:
        overlay = _overlay(url)
        joined = rig.join(
            LaneHealthLabPassReceipts(overlay, fetch=_projection(rows)),
            HttpRuntimeProbe(overlay),
            at,
        ).handle(_request(cid, at))
    assert joined.outcome is EnumJoinJ10Outcome.JOINED
    assert joined.runtime_observed_sha == MERGED
    assert [e.phase for e in rig.piece11()] == ["start", "success"]


def test_golden_a_merge_that_is_not_runtime_affecting_needs_neither_and_emits_nothing() -> (
    None
):
    rig = _Rig()
    runtime = InMemoryRuntime(PARENT)
    at = MERGED_AT + HOUR + timedelta(minutes=5)
    result = rig.join(InMemoryLabPassReceipts(), runtime, at).handle(
        _request("j10-exempt", at, runtime_affecting=False)
    )
    assert result.outcome is EnumJoinJ10Outcome.EXEMPT
    assert runtime.asked == []
    assert rig.sink.events == []


def test_golden_no_receipt_yet_inside_the_deadline_is_pending_not_failed() -> None:
    rig = _Rig()
    at = MERGED_AT + timedelta(minutes=10)
    result = rig.join(InMemoryLabPassReceipts(), InMemoryRuntime(PARENT), at).handle(
        _request("j10-pending", at)
    )
    assert result.outcome is EnumJoinJ10Outcome.PENDING
    assert result.failure_code == POLICY.receipt_missing_code
    assert rig.sink.events == [], "an unjudged join emits nothing"


def test_golden_a_runtime_still_rebuilding_inside_the_deadline_is_pending_not_failed() -> (
    None
):
    rig = _Rig()
    at = MERGED_AT + timedelta(minutes=30)
    result = rig.join(_ok_receipts(), InMemoryRuntime(PARENT), at).handle(
        _request("j10-rebuilding", at)
    )
    assert result.outcome is EnumJoinJ10Outcome.PENDING
    assert result.failure_code == POLICY.runtime_stale_code
    assert rig.sink.events == []


# ---------------------------------------------------------------------------------------------- error


def _failed(rig: _Rig, receipts: Any, runtime: Any, cid: str) -> ModelJoinJ10Result:
    judged_at = MERGED_AT + HOUR + timedelta(seconds=1)
    return rig.join(receipts, runtime, judged_at).handle(_request(cid, judged_at))


def _assert_piece11_failed(rig: _Rig, code: str) -> None:
    piece11 = rig.piece11()
    assert [e.phase for e in piece11] == ["start", "failure"]
    assert piece11[-1].failure_code == code


def test_error_a_pass_receipt_beside_a_stale_runtime_fails_the_join() -> None:
    rig = _Rig()
    cid = "j10-stale"
    rig.piece10_delivered(cid)
    runtime = InMemoryRuntime(PARENT)
    result = _failed(rig, _ok_receipts(), runtime, cid)
    assert result.outcome is EnumJoinJ10Outcome.FAILED
    assert result.failure_code == POLICY.runtime_stale_code
    assert result.runtime_observed_sha == PARENT
    assert MERGED[:12] in result.failure_detail
    assert PARENT[:12] in result.failure_detail
    _assert_piece11_failed(rig, POLICY.runtime_stale_code)


def test_error_a_pass_receipt_beside_a_stale_runtime_served_over_http_fails_the_join() -> (
    None
):
    rig = _Rig()
    rows = [
        {
            "lane": LANE,
            "receipt_sha": MERGED,
            "receipt_result": "PASS",
            "receipt_observed_at": (MERGED_AT + timedelta(minutes=20)).isoformat(),
        }
    ]
    with _serve(PARENT) as url:
        overlay = _overlay(url)
        result = _failed(
            rig,
            LaneHealthLabPassReceipts(overlay, fetch=_projection(rows)),
            HttpRuntimeProbe(overlay),
            "j10-stale-http",
        )
    assert result.outcome is EnumJoinJ10Outcome.FAILED
    assert result.failure_code == POLICY.runtime_stale_code
    _assert_piece11_failed(rig, POLICY.runtime_stale_code)


@pytest.mark.parametrize(
    "served",
    [
        pytest.param({"status": 500, "sha": MERGED}, id="http-500"),
        pytest.param({"status": 200, "sha": MERGED, "ok": False}, id="not-functional"),
        pytest.param({"status": 200, "sha": None}, id="no-sha"),
    ],
)
def test_error_a_pass_receipt_beside_a_broken_runtime_fails_the_join(
    served: dict[str, Any],
) -> None:
    rig = _Rig()
    with _serve(
        served["sha"], status=served["status"], ok=served.get("ok", True)
    ) as url:
        overlay = _overlay(url)
        result = _failed(rig, _ok_receipts(), HttpRuntimeProbe(overlay), "j10-broken")
    assert result.outcome is EnumJoinJ10Outcome.FAILED
    assert result.failure_code == POLICY.runtime_broken_code
    _assert_piece11_failed(rig, POLICY.runtime_broken_code)


def test_error_a_runtime_that_is_down_fails_the_join_as_broken() -> None:
    rig = _Rig()
    runtime = InMemoryRuntime(None, reachable=False)
    result = _failed(rig, _ok_receipts(), runtime, "j10-down")
    assert result.outcome is EnumJoinJ10Outcome.FAILED
    assert result.failure_code == POLICY.runtime_broken_code
    assert "no answer" in result.failure_detail


def test_error_a_connection_refused_is_a_broken_runtime_not_a_raise() -> None:
    rig = _Rig()
    overlay = _overlay("http://127.0.0.1:1/runtime")
    result = _failed(rig, _ok_receipts(), HttpRuntimeProbe(overlay), "j10-refused")
    assert result.outcome is EnumJoinJ10Outcome.FAILED
    assert result.failure_code == POLICY.runtime_broken_code


def test_error_a_runtime_that_does_not_echo_the_correlation_id_fails_the_join() -> None:
    rig = _Rig()
    with _serve(MERGED, echo=False) as url:
        overlay = _overlay(url)
        result = _failed(
            rig, _ok_receipts(), HttpRuntimeProbe(overlay), "j10-uncorrelated"
        )
    assert result.outcome is EnumJoinJ10Outcome.FAILED
    assert result.failure_code == POLICY.runtime_uncorrelated_code
    _assert_piece11_failed(rig, POLICY.runtime_uncorrelated_code)


def test_error_a_runtime_beside_no_receipt_past_the_deadline_is_named_missing() -> None:
    rig = _Rig()
    runtime = InMemoryRuntime(MERGED)
    result = _failed(rig, InMemoryLabPassReceipts(), runtime, "j10-no-receipt")
    assert result.outcome is EnumJoinJ10Outcome.FAILED
    assert result.failure_code == POLICY.receipt_missing_code
    assert runtime.asked == [], "a missing receipt is not rescued by a healthy runtime"
    _assert_piece11_failed(rig, POLICY.receipt_missing_code)


def test_error_a_receipt_for_another_sha_or_lane_is_no_receipt() -> None:
    rig = _Rig()
    store = InMemoryLabPassReceipts()
    store.add(_receipt(PARENT))
    store.add(_receipt(MERGED, lane="another-lane"))
    result = _failed(rig, store, InMemoryRuntime(MERGED), "j10-other-sha")
    assert result.outcome is EnumJoinJ10Outcome.FAILED
    assert result.failure_code == POLICY.receipt_missing_code


def test_error_a_fail_receipt_fails_the_join_even_beside_a_healthy_runtime() -> None:
    rig = _Rig()
    store = InMemoryLabPassReceipts()
    store.add(_receipt(result="FAIL"))
    result = _failed(rig, store, InMemoryRuntime(MERGED), "j10-fail-receipt")
    assert result.outcome is EnumJoinJ10Outcome.FAILED
    assert result.failure_code == POLICY.receipt_failed_code


def test_error_a_pass_receipt_after_the_deadline_is_late() -> None:
    rig = _Rig()
    store = InMemoryLabPassReceipts()
    store.add(_receipt(minutes=90))
    judged_at = MERGED_AT + timedelta(minutes=120)
    result = rig.join(store, InMemoryRuntime(MERGED), judged_at).handle(
        _request("j10-late", judged_at)
    )
    assert result.outcome is EnumJoinJ10Outcome.FAILED
    assert result.failure_code == POLICY.receipt_late_code


def test_error_a_receipt_from_before_the_merge_does_not_count() -> None:
    rig = _Rig()
    store = InMemoryLabPassReceipts()
    store.add(_receipt(minutes=-5))
    result = _failed(rig, store, InMemoryRuntime(MERGED), "j10-before-merge")
    assert result.outcome is EnumJoinJ10Outcome.FAILED
    assert result.failure_code == POLICY.receipt_missing_code


def test_error_an_unreadable_receipt_source_is_named_and_never_read_as_a_pass() -> None:
    rig = _Rig()
    store = InMemoryLabPassReceipts(unread_reason="projection returned 503")
    result = _failed(rig, store, InMemoryRuntime(MERGED), "j10-unread")
    assert result.outcome is EnumJoinJ10Outcome.FAILED
    assert result.failure_code == POLICY.receipts_unreadable_code
    assert "503" in result.failure_detail


def test_error_the_lane_health_projection_failing_to_load_reads_unreadable() -> None:
    def fetch(url: str, headers: dict[str, str], timeout: int) -> str:
        raise OSError("connection refused")

    overlay = _overlay("http://127.0.0.1:1/runtime")
    read = LaneHealthLabPassReceipts(overlay, fetch=fetch).read_receipts(MERGED)
    assert read.receipts == ()
    assert read.unread_reason is not None
    assert "connection refused" in read.unread_reason
