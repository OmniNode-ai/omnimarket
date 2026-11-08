# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 (OMN-22958): the reading is NOT WORKING when no PASS receipt exists within an hour of a
runtime-affecting merge, or the 24-hour probe is missing or failing."""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.models.model_reading import EnumReadingVerdict as V
from omnimarket.nodes.node_dev_deploy_reading_compute.handlers.handler_dev_deploy_reading import (
    HandlerDevDeployReading,
)
from omnimarket.nodes.node_dev_deploy_reading_compute.models.model_dev_deploy_reading import (
    ModelDevDeployReading,
)
from tests.nodes.node_dev_deploy_reading_compute.dev_deploy_support import (
    LANE,
    SHA_A,
    SHA_B,
    SHA_C,
    good,
    merge,
    probe,
    receipt,
    request,
)


def _read(**edits: Any) -> ModelDevDeployReading:
    return HandlerDevDeployReading().handle(request(**edits))


def _reasons(**edits: Any) -> tuple[str, ...]:
    return _read(**edits).reasons


def test_receipt_and_probe_good_request_reads_working() -> None:
    reading = _read()
    assert reading.verdict is V.WORKING
    assert reading.reasons == ()
    assert (reading.merges_checked, reading.merges_receipted) == (2, 2)
    assert reading.probe_age_seconds == 6 * 3600


def test_receipt_and_probe_missing_pass_receipt_past_the_hour_is_not_working() -> None:
    receipts = [receipt(SHA_B, "2026-11-08T09:10:00Z")]
    reading = _read(receipts=receipts)
    assert reading.verdict is V.NOT_WORKING
    assert reading.reasons == (f"no-pass-receipt:{SHA_A[:12]}:age-14400s-over-3600s",)


def test_receipt_and_probe_merge_inside_its_hour_is_pending_and_fails_nothing() -> None:
    merges = [merge(SHA_A, "2026-11-08T11:30:00Z")]
    reading = _read(merges=merges, receipts=[])
    assert reading.verdict is V.WORKING
    assert reading.pending == (f"receipt-pending:{SHA_A[:12]}",)


def test_receipt_and_probe_exactly_one_hour_old_merge_is_still_pending() -> None:
    merges = [merge(SHA_A, "2026-11-08T11:00:00Z")]
    assert _read(merges=merges, receipts=[]).verdict is V.WORKING
    merges = [merge(SHA_A, "2026-11-08T10:59:59Z")]
    assert _read(merges=merges, receipts=[]).verdict is V.NOT_WORKING


def test_receipt_and_probe_non_runtime_merge_needs_no_receipt() -> None:
    merges = [merge(SHA_C, "2026-11-08T01:00:00Z", runtime_affecting=False)]
    reading = _read(merges=merges, receipts=[])
    assert reading.verdict is V.WORKING
    assert reading.merges_checked == 0


def test_receipt_and_probe_fail_receipt_is_no_pass_receipt() -> None:
    receipts = [
        receipt(SHA_A, "2026-11-08T08:20:00Z", result="FAIL"),
        receipt(SHA_B, "2026-11-08T09:20:00Z"),
    ]
    assert _reasons(receipts=receipts) == (
        f"no-pass-receipt:{SHA_A[:12]}:age-14400s-over-3600s",
        f"receipt-fail:{SHA_A[:12]}",
    )


def test_receipt_and_probe_a_pass_after_the_hour_is_late() -> None:
    receipts = [
        receipt(SHA_A, "2026-11-08T09:00:01Z"),
        receipt(SHA_B, "2026-11-08T09:20:00Z"),
    ]
    assert _reasons(receipts=receipts) == (
        f"receipt-late:{SHA_A[:12]}:3601s-over-3600s",
    )


def test_receipt_and_probe_a_pass_for_another_sha_or_lane_is_no_evidence() -> None:
    other_sha = [
        receipt(SHA_C, "2026-11-08T08:20:00Z"),
        receipt(SHA_B, "2026-11-08T09:20:00Z"),
    ]
    assert _reasons(receipts=other_sha) == (
        f"no-pass-receipt:{SHA_A[:12]}:age-14400s-over-3600s",
    )
    other_lane = [
        receipt(SHA_A, "2026-11-08T08:20:00Z", lane="lane-b"),
        receipt(SHA_B, "2026-11-08T09:20:00Z"),
    ]
    assert _reasons(receipts=other_lane) == (
        f"no-pass-receipt:{SHA_A[:12]}:age-14400s-over-3600s",
    )
    assert LANE != "lane-b"


def test_receipt_and_probe_receipt_finished_before_the_merge_is_refused_as_evidence() -> (
    None
):
    receipts = [
        receipt(SHA_A, "2026-11-08T07:00:00Z"),
        receipt(SHA_B, "2026-11-08T09:20:00Z"),
    ]
    assert _reasons(receipts=receipts) == (f"receipt-before-merge:{SHA_A[:12]}",)


def test_receipt_and_probe_missing_probe_is_not_working() -> None:
    reading = _read(probes=[])
    assert reading.verdict is V.NOT_WORKING
    assert reading.reasons == ("probe-missing",)


def test_receipt_and_probe_stale_probe_is_not_working() -> None:
    reasons = _reasons(probes=[probe(probed_at="2026-11-07T11:59:59Z")])
    assert reasons == ("probe-stale:probe-1:age-86401s-over-86400s",)
    assert _read(probes=[probe(probed_at="2026-11-07T12:00:00Z")]).verdict is V.WORKING


def test_receipt_and_probe_failing_probe_is_not_working() -> None:
    assert _reasons(probes=[probe(ok=False)]) == ("probe-failed:probe-1",)


def test_receipt_and_probe_probe_that_saw_another_sha_is_not_working() -> None:
    reasons = _reasons(probes=[probe(expected=SHA_B, observed=SHA_A)])
    assert reasons == (
        f"probe-sha-mismatch:probe-1:{SHA_B[:12]}-observed-{SHA_A[:12]}",
    )
    assert _reasons(probes=[probe(observed=None)]) == ("probe-sha-unobserved:probe-1",)


def test_receipt_and_probe_future_dated_probe_is_not_working() -> None:
    assert _reasons(probes=[probe(probed_at="2026-11-08T12:00:01Z")]) == (
        "probe-future-dated:probe-1",
    )


def test_receipt_and_probe_newest_probe_decides() -> None:
    older_fail = probe(probed_at="2026-11-08T01:00:00Z", ok=False, probe_id="old")
    newer_pass = probe(probed_at="2026-11-08T05:00:00Z", probe_id="new")
    assert _read(probes=[older_fail, newer_pass]).verdict is V.WORKING
    newer_fail = probe(probed_at="2026-11-08T05:00:00Z", ok=False, probe_id="new")
    older_pass = probe(probed_at="2026-11-08T01:00:00Z", probe_id="old")
    assert _reasons(probes=[older_pass, newer_fail]) == ("probe-failed:new",)


def test_receipt_and_probe_unreadable_source_is_unknown_unless_the_other_fails() -> (
    None
):
    reading = _read(receipts_unread_reason="actions-artifact-api-503")
    assert reading.verdict is V.UNKNOWN
    assert reading.reasons == ("receipts-unreadable:actions-artifact-api-503",)
    both = _read(probes=[], receipts_unread_reason="x")
    assert both.verdict is V.NOT_WORKING
    assert both.reasons == ("probe-missing", "receipts-unreadable:x")


def test_receipt_and_probe_a_read_is_a_pure_function_of_the_request() -> None:
    handler = HandlerDevDeployReading()
    assert handler.handle(request()) == handler.handle(request())


@pytest.mark.parametrize(
    ("edits", "match"),
    [
        ({"now": "2026-11-08T12:00:00+01:00"}, "is not UTC"),
        ({"now": "2026-11-08T12:00:00"}, "timezone"),
        ({"receipt_deadline_seconds": 0}, "greater than 0"),
        ({"probe_max_age_seconds": 0}, "greater than 0"),
        ({"required_lane": ""}, "at least 1"),
        ({"receipts_unread_reason": "  "}, "names no reason"),
        ({"merges": [merge(SHA_A, "2026-11-08T08:00:00Z")] * 2}, "listed twice"),
        ({"merges": [merge("abc", "2026-11-08T08:00:00Z")]}, "not an exact sha"),
        (
            {"merges": [merge(SHA_A.upper(), "2026-11-08T08:00:00Z")]},
            "not an exact sha",
        ),
    ],
)
def test_receipt_and_probe_malformed_request_is_refused_by_name(
    edits: dict[str, Any], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        request(**edits)
    assert request().now is not None  # positive control: the good request validates
    assert good()["required_lane"] == LANE


def test_receipt_and_probe_fractional_future_dated_probe_is_not_working() -> None:
    assert _reasons(probes=[probe(probed_at="2026-11-08T12:00:00.500Z")]) == (
        "probe-future-dated:probe-1",
    )


def test_receipt_and_probe_probe_a_fraction_past_max_age_is_stale() -> None:
    reasons = _reasons(probes=[probe(probed_at="2026-11-07T11:59:59.500Z")])
    assert reasons == ("probe-stale:probe-1:age-86400s-over-86400s",)
    exactly = probe(probed_at="2026-11-07T12:00:00Z")
    assert (
        _read(probes=[exactly]).verdict is V.WORKING
    )  # control: exactly max age holds


def test_receipt_and_probe_a_later_qualifying_pass_masks_an_earlier_ineligible_one() -> (
    None
):
    receipts = [
        receipt(SHA_A, "2026-11-08T07:50:00Z"),
        receipt(SHA_A, "2026-11-08T08:20:00Z"),
        receipt(SHA_B, "2026-11-08T09:20:00Z"),
    ]
    reading = _read(receipts=receipts)
    assert reading.verdict is V.WORKING
    assert reading.reasons == ()
    assert reading.merges_receipted == 2


def test_receipt_and_probe_an_ineligible_pass_does_not_fail_a_pending_merge() -> None:
    merges = [merge(SHA_A, "2026-11-08T11:30:00Z")]
    receipts = [receipt(SHA_A, "2026-11-08T11:20:00Z")]
    reading = _read(merges=merges, receipts=receipts)
    assert reading.verdict is V.WORKING
    assert reading.pending == (f"receipt-pending:{SHA_A[:12]}",)
    assert reading.merges_receipted == 0


def test_receipt_and_probe_a_receipt_finishing_after_the_read_time_is_no_evidence() -> (
    None
):
    merges = [merge(SHA_A, "2026-11-08T11:30:00Z")]
    for finished in ("2026-11-08T12:45:00Z", "2026-11-08T12:15:00Z"):
        reading = _read(merges=merges, receipts=[receipt(SHA_A, finished)])
        assert reading.verdict is V.WORKING
        assert reading.pending == (f"receipt-pending:{SHA_A[:12]}",)
        assert reading.merges_receipted == 0
    on_time = _read(merges=merges, receipts=[receipt(SHA_A, "2026-11-08T11:45:00Z")])
    assert (
        on_time.merges_receipted == 1
    )  # control: a receipt finished by the read counts


def test_receipt_and_probe_a_trailing_newline_is_not_an_exact_sha() -> None:
    with pytest.raises(ValueError, match="not an exact sha"):
        request(merges=[merge(SHA_A + "\n", "2026-11-08T08:00:00Z")])
    with pytest.raises(ValueError, match="not an exact sha"):
        request(receipts=[receipt(SHA_A + "\n", "2026-11-08T08:20:00Z")])
    with pytest.raises(ValueError, match="not an exact sha"):
        request(probes=[probe(observed=SHA_B + "\n")])
