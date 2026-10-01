# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20299: the access-log join names bypasses and attributes runs."""

from omnimarket.model_call_reconcile.reconcile import (
    EnumServedRequestClass,
    build_report,
    classify,
    parse_access_log,
    parse_access_log_line,
)

_CID = "11111111-1111-4111-8111-111111111111"


def _line(path: str, status: int = 200) -> str:
    return (
        "2026-10-01T13:36:45-04:00 omninode-pc python[1]: (APIServer pid=1) "
        f'INFO:     172.19.0.27:48778 - "POST {path} HTTP/1.1" {status} OK'
    )


def test_parse_extracts_cid_and_timestamp() -> None:
    req = parse_access_log_line(
        _line(f"/v1/chat/completions?onex_cid={_CID}"), "201:8000"
    )
    assert req is not None
    assert req.correlation_id == _CID
    assert req.timestamp == "2026-10-01T13:36:45-04:00"


def test_parse_skips_non_inference_and_get() -> None:
    assert parse_access_log_line(_line("/v1/embeddings"), "s") is None
    assert parse_access_log_line('INFO: x - "GET /health HTTP/1.1" 200 OK', "s") is None


def test_cidless_request_is_a_bypass() -> None:
    reqs = parse_access_log([_line("/v1/messages?beta=true", 500)], "201:8000")
    [item] = classify(reqs, {})
    assert item.classification is EnumServedRequestClass.BYPASS


def test_report_splits_attributed_orphan_bypass() -> None:
    lines = [
        _line(f"/v1/chat/completions?onex_cid={_CID}"),
        _line("/v1/chat/completions?onex_cid=unknown"),
        _line("/v1/chat/completions"),
    ]
    report = build_report(classify(parse_access_log(lines, "201:8000"), {_CID: None}))
    assert (report.total, report.attributed, report.orphan_correlation_id) == (3, 1, 1)
    assert report.bypass == 1
    assert report.attributed_by_lane == {"unattributed": 1}
    assert report.bypass_by_server == {"201:8000": 1}
