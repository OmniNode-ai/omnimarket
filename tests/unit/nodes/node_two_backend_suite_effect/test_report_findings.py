# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22911 review findings: incomplete inventories, undecodable reports and a stalled backend are visible failures.

Each test names its break: a report that is ``{}``, a report that lists unreadable paths, a report file that is not UTF-8,
and a scenario that does not return inside the bound.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from omnimarket.nodes.node_two_backend_suite_effect.adapters.report_sources import (
    FileReportSource,
    InMemoryReportSource,
)
from omnimarket.nodes.node_two_backend_suite_effect.handlers.handler_two_backend_suite import (
    HandlerTwoBackendSuite,
)
from omnimarket.nodes.node_two_backend_suite_effect.models.model_two_backend_suite import (
    EnumTwoBackendVerdict,
    ModelEffectBackendEntry,
    ModelEffectBackendReport,
    ModelTwoBackendRequest,
)

pytestmark = pytest.mark.unit

REQUEST = ModelTwoBackendRequest(correlation_id="c-findings")


class _NeverUsed:
    def resolve(self, dotted: str) -> Callable[..., object]:
        raise AssertionError("no effect may run when the inventory is incomplete")


def _file_report(tmp_path: Path, content: bytes) -> FileReportSource:
    path = tmp_path / "report.json"
    path.write_bytes(content)
    return FileReportSource(path)


def test_error_an_empty_object_is_not_a_complete_inventory(tmp_path: Path) -> None:
    read = _file_report(tmp_path, b"{}").read()
    assert read.report is None
    assert read.error == "report-unreadable:ValidationError"


def test_golden_an_explicitly_empty_inventory_is_complete(tmp_path: Path) -> None:
    read = _file_report(tmp_path, b'{"entries": []}').read()
    assert read.error is None
    assert read.report is not None
    assert read.report.entries == ()


def test_error_a_report_listing_unreadable_paths_fails_the_suite(
    tmp_path: Path,
) -> None:
    report = ModelEffectBackendReport(entries=(), unreadable=("node_x/contract.yaml",))
    handler = HandlerTwoBackendSuite(
        source=InMemoryReportSource(report), resolver=_NeverUsed(), workdir=tmp_path
    )
    result = handler.handle(REQUEST)
    assert result.status == "failed"
    assert result.failure == "report-incomplete:1-unreadable"


def test_error_a_file_that_is_not_utf8_is_an_unreadable_report(tmp_path: Path) -> None:
    read = _file_report(tmp_path, b"\xff\xfe\x00not utf-8").read()
    assert read.report is None
    assert read.error == "report-unreadable:UnicodeDecodeError"


class _StallingResolver:
    """A scenario for the effect ``slow`` that does not return inside the bound; every other effect passes."""

    def __init__(self, release: threading.Event) -> None:
        self._release = release

    def resolve(self, dotted: str) -> Callable[..., object]:
        if dotted == "scenario.slow":
            return self._slow
        if dotted == "scenario.fast":
            return self._fast
        return self._backend

    def _slow(self, factory: object, workdir: Path) -> bool:
        return self._release.wait(timeout=5)

    def _fast(self, factory: object, workdir: Path) -> str:
        return "ok"

    def _backend(self, rows: object, workdir: Path) -> object:
        return object()


def _entry(name: str) -> ModelEffectBackendEntry:
    return ModelEffectBackendEntry(
        node_name=name,
        has_in_memory_default=True,
        has_real_backend_overlay=True,
        in_memory_factory="factory.memory",
        real_factory="factory.real",
        scenario=f"scenario.{name}",
    )


def test_error_a_stalled_backend_times_out_and_later_effects_still_run(
    tmp_path: Path,
) -> None:
    release = threading.Event()
    try:
        handler = HandlerTwoBackendSuite(
            source=InMemoryReportSource(
                ModelEffectBackendReport(entries=(_entry("slow"), _entry("fast")))
            ),
            resolver=_StallingResolver(release),
            workdir=tmp_path,
            timeout_s=0.2,
        )
        result = handler.handle(REQUEST)
    finally:
        release.set()
    by_name = {r.node_name: r for r in result.runs}
    assert by_name["slow"].in_memory == "fail:TimeoutError"
    assert by_name["slow"].real == "fail:TimeoutError"
    assert by_name["slow"].verdict is EnumTwoBackendVerdict.BOTH_FAIL
    assert by_name["fast"].verdict is EnumTwoBackendVerdict.BOTH_PASS
