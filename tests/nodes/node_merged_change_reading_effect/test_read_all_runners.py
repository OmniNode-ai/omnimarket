# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``read --all-runners`` reads every change the runner overlay declares, once, with the explicit changes first."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest

from omnimarket.models.model_merged_change_reading import ModelMcrRunner
from omnimarket.nodes.node_merged_change_reading_effect import (
    runtime_merged_change_reading as runtime,
)
from omnimarket.nodes.node_merged_change_reading_effect.sources.source_in_memory import (
    SourceInMemoryChangeReading,
)

pytestmark = pytest.mark.unit

_RUNNERS = (
    ModelMcrRunner(repo="omnimarket", number=6193, kind="host_unit"),
    ModelMcrRunner(repo="omnimarket", number=6120, kind="library"),
)


def _patch_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    def build(environ: Any, now: datetime, *, home: Any = None) -> Any:
        return SourceInMemoryChangeReading(runners=_RUNNERS)

    monkeypatch.setattr(runtime, "build_sources", build)


def _numbers(capsys: pytest.CaptureFixture[str]) -> list[int]:
    out = json.loads(capsys.readouterr().out)
    return [row["number"] for row in out["reading"]["readings"]]


def test_all_runners_reads_every_declared_change(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_sources(monkeypatch)
    assert runtime.main(["read", "--all-runners"]) == 0
    assert _numbers(capsys) == [6193, 6120]


def test_an_explicit_change_is_read_once_beside_all_runners(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_sources(monkeypatch)
    argv = [
        "read",
        "--all-runners",
        "--change",
        "omnimarket#6120",
        "--change",
        "omnimarket#7",
    ]
    assert runtime.main(argv) == 0
    assert _numbers(capsys) == [6120, 7, 6193]


def test_with_no_change_and_no_all_runners_it_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sources(monkeypatch)
    with pytest.raises(SystemExit) as stopped:
        runtime.main(["read"])
    assert stopped.value.code == 2
