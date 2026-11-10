# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A host with no /proc (macOS) reads live working directories with lsof, so its sweep closes lanes (OMN-23404)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omnimarket.nodes.node_lane_closeout_sweep_effect.handlers import (
    handler_lane_closeout_sweep as handler,
)
from omnimarket.nodes.node_lane_closeout_sweep_effect.handlers.handler_lane_closeout_sweep import (
    HandlerLaneCloseoutSweep,
)
from tests.nodes.node_lane_closeout_sweep_effect.test_golden_chain_lane_closeout_sweep import (
    FILL_LANE,
    Host,
    request,
)

pytestmark = pytest.mark.unit

REAL_RUN = subprocess.run
LSOF_OUT = "p176\nfcwd\nn/\np389\nfcwd\nn/Users/x/lab-run/runs/rlane-a-1a2b3c\n"


def fake_lsof(stdout: str, code: int = 0) -> object:
    def run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        if argv[0] != "lsof":  # the sweep's git calls share the subprocess module
            return REAL_RUN(argv, **kw)  # type: ignore[no-any-return,call-overload]
        assert argv[:2] == ["lsof", "-a"]
        return subprocess.CompletedProcess(argv, code, stdout, "")

    return run


def test_lsof_output_is_read_as_working_directories() -> None:
    assert handler.parse_lsof_cwds(LSOF_OUT) == [
        "/",
        "/Users/x/lab-run/runs/rlane-a-1a2b3c",
    ]


def test_lsof_partial_exit_still_counts_the_lines_it_printed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handler.subprocess, "run", fake_lsof(LSOF_OUT, code=1))
    assert handler._lsof_cwds() == ["/", "/Users/x/lab-run/runs/rlane-a-1a2b3c"]


def test_a_host_that_cannot_run_lsof_says_it_cannot_say(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failing control: no reading, so the sweep leaves every lane alone (None), never closes one blind."""

    def missing(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        if argv[0] != "lsof":
            return REAL_RUN(argv, **kw)  # type: ignore[no-any-return,call-overload]
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(handler.subprocess, "run", missing)
    assert handler._lsof_cwds() is None
    monkeypatch.setattr(handler.subprocess, "run", fake_lsof("", code=2))
    assert handler._lsof_cwds() is None


def test_the_sweep_skips_a_lane_an_lsof_read_shows_in_use_and_closes_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = Host(tmp_path)
    tree = host.lane(FILL_LANE)
    monkeypatch.setattr(
        handler.subprocess, "run", fake_lsof(f"p1\nfcwd\nn{tree}/sub\n")
    )
    used = HandlerLaneCloseoutSweep(live_cwds=handler._lsof_cwds).handle(
        request(tmp_path)
    )
    assert tree.exists()
    assert used.skipped_live == (FILL_LANE,)
    monkeypatch.setattr(handler.subprocess, "run", fake_lsof("p1\nfcwd\nn/\n"))
    freed = HandlerLaneCloseoutSweep(live_cwds=handler._lsof_cwds).handle(
        request(tmp_path)
    )
    assert not tree.exists()
    assert [r.lane for r in freed.closed] == [FILL_LANE]
