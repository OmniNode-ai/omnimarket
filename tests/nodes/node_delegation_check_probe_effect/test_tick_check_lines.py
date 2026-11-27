# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The probe tick prints one line per check: check id, verdict and correlation id; a failed publish is loud (OMN-26723)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from _gateway_fake import FakeGateway
from test_tick_single_check import _overlays

from omnimarket.nodes.node_delegation_check_probe_effect import (
    runtime_delegation_check_probe_tick as tick,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_delegation_check import (
    InMemoryCheckPublisher,
)

LINE = re.compile(
    r"^check=(C\d+) verdict=(\S+) correlation_id=(\S+) published=(yes|no)$"
)


def _tick(tmp_path: Path, mp: pytest.MonkeyPatch, pub: InMemoryCheckPublisher) -> int:
    for k, v in _overlays(tmp_path, ["C13", "C14", "C17"]).items():
        mp.setenv(k, v)
    return tick.main(
        ["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "state")],
        publisher=pub,
        gateway=FakeGateway(),
    )


def test_one_line_per_check_names_id_verdict_and_correlation_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    pub = InMemoryCheckPublisher()
    _tick(tmp_path, monkeypatch, pub)
    lines = [m for m in map(LINE.match, capfd.readouterr().out.splitlines()) if m]
    assert [m.group(1) for m in lines] == ["C13", "C14", "C17"]
    sent = {e["payload"]["criterion"]: e["correlation_id"] for e in pub.events}
    assert {m.group(1): m.group(3) for m in lines} == sent
    assert all(m.group(4) == "yes" for m in lines)


def test_a_refused_publish_is_loud_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    code = _tick(tmp_path, monkeypatch, InMemoryCheckPublisher(accept=False))
    cap = capfd.readouterr()
    assert code == tick.EXIT_NOT_PUBLISHED
    assert "published=no" in cap.out
    assert cap.err.count("NOT PUBLISHED") == 3
