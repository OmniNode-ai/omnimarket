# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The per-host rules of piece 10's reading (OMN-22956): fresh, whole, equal to the head's file list."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.model_reading import EnumReadingVerdict as V
from omnimarket.nodes.node_install_reading_compute.handlers.handler_install_reading import (
    HandlerInstallReading,
)
from omnimarket.nodes.node_install_reading_compute.models.model_install_reading import (
    ModelInstallReading,
)
from tests.nodes.node_install_reading_compute.install_support import (
    HOST_A,
    HOST_B,
    ROOT_DATA,
    probe,
    request,
)

pytestmark = pytest.mark.unit


def _read(**edits: Any) -> ModelInstallReading:
    return HandlerInstallReading().handle(request(**edits))


def test_probe_rules_a_good_request_reads_working() -> None:
    reading = _read()
    assert reading.verdict is V.WORKING
    assert reading.reasons == ()
    assert (reading.hosts_checked, reading.hosts_whole) == (2, 2)


def test_probe_rules_a_required_host_with_no_probe_reads_not_working() -> None:
    reading = _read(probes=[probe(HOST_A)])
    assert reading.verdict is V.NOT_WORKING
    assert reading.reasons == (f"probe-missing:{HOST_B}",)
    assert reading.hosts_whole == 1


def test_probe_rules_a_probe_of_a_host_not_required_is_no_evidence() -> None:
    reading = _read(probes=[probe(HOST_A), probe("host-z")])
    assert reading.reasons == (f"probe-missing:{HOST_B}",)


def test_probe_rules_a_stale_probe_reads_not_working() -> None:
    reading = _read(
        probes=[probe(HOST_A), probe(HOST_B, probed_at="2026-11-07T00:00:00Z")]
    )
    assert reading.verdict is V.NOT_WORKING
    assert reading.reasons == (f"probe-stale:{HOST_B}:age-216000s-over-86400s",)


def test_probe_rules_a_future_dated_probe_reads_not_working() -> None:
    reading = _read(
        probes=[probe(HOST_A), probe(HOST_B, probed_at="2026-11-09T12:00:01Z")]
    )
    assert reading.reasons == (f"probe-future-dated:{HOST_B}",)


def test_probe_rules_an_empty_file_list_reads_not_working() -> None:
    reading = _read(probes=[probe(HOST_A), probe(HOST_B, expected=0)])
    assert reading.reasons == (f"file-list-empty:{HOST_B}",)


def test_probe_rules_a_missing_file_reads_not_working_and_names_it() -> None:
    reading = _read(probes=[probe(HOST_A, missing=("b.json", "a.json")), probe(HOST_B)])
    assert reading.verdict is V.NOT_WORKING
    assert reading.reasons == (f"install-missing:{HOST_A}:2:a.json",)


def test_probe_rules_only_the_newest_probe_of_a_host_counts() -> None:
    stale_bad = probe(HOST_A, probed_at="2026-11-09T01:00:00Z", missing=("a.json",))
    newer_good = probe(HOST_A, probed_at="2026-11-09T09:00:00Z")
    assert _read(probes=[stale_bad, newer_good, probe(HOST_B)]).verdict is V.WORKING
    assert _read(probes=[newer_good, stale_bad, probe(HOST_B)]).verdict is V.WORKING
    older_good = probe(HOST_A, probed_at="2026-11-09T01:00:00Z")
    newer_bad = probe(HOST_A, probed_at="2026-11-09T09:00:00Z", missing=("a.json",))
    assert _read(probes=[older_good, newer_bad, probe(HOST_B)]).verdict is V.NOT_WORKING


def test_probe_rules_unreadable_probes_read_unknown_unless_something_readable_fails() -> (
    None
):
    unread = _read(probes=[], probes_unread_reason="canary-record-missing")
    assert unread.verdict is V.UNKNOWN
    assert unread.reasons == ("probes-unreadable:canary-record-missing",)
    assert unread.hosts_whole == 0


def test_probe_rules_a_request_that_cannot_be_read_as_asked_is_refused() -> None:
    for bad in (
        probe(trigger="drill"),
        probe(fault=ROOT_DATA),
        probe(trigger="hourly"),
        probe(expected=1, missing=("a.json", "b.json")),
        probe(missing=("a.json", "a.json")),
        probe(probed_at="2026-11-09T06:00:00+02:00"),
    ):
        with pytest.raises(ValidationError):
            request(probes=[bad])
    with pytest.raises(ValidationError, match="listed twice"):
        request(required_hosts=[HOST_A, HOST_A])
    with pytest.raises(ValidationError, match="names no reason"):
        request(probes_unread_reason=" ")
