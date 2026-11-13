# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick's typed limit-recheck outcome per limited label and the line it prints (OMN-23955).

The golden pass shows the typed pair (a cleared label and a kept one, each stamped with the probe time), the
failing controls show a limited or error answer never reports cleared, a label inside the probe interval reports
not_due, a dry run reports due and asks nothing, an overlay without the block reports not_configured with no
outcomes, and the printed line carries the typed outcomes (an error chain prints the status and no outcomes).
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelClaudeTokenRotationResult,
    ModelLimitRecheckOutcome,
    ModelRotationMarksSync,
)
from omnimarket.nodes.node_claude_token_pool_effect.runtime_claude_pool_tick import (
    HOST_ENV,
    main,
)

from .test_limit_recheck import (
    INTERVAL,
    NOW,
    REAL_RESET,
    STALE_STAMP,
    FakeProbe,
    Rig,
    answer,
)

pytestmark = pytest.mark.unit

MODULE = "omnimarket.nodes.node_claude_token_pool_effect.runtime_claude_pool_tick"


def test_golden_typed_outcomes_cleared_and_kept(tmp_path: Path) -> None:
    probe = FakeProbe(
        {
            "onexbot": answer("ok"),
            "caia": answer("limited", REAL_RESET),
        }
    )
    rig = Rig(tmp_path, "h053", probe)
    rig.limit("onexbot", STALE_STAMP)
    rig.limit("caia", "2026-11-14T04:00:00Z")
    result = rig.tick()
    assert result.limit_recheck == "evaluated"
    got = {o.label: o for o in result.limit_rechecks}
    assert got["onexbot"].outcome == "cleared"
    assert got["onexbot"].probed_at == NOW
    assert got["caia"].outcome == "kept"
    assert got["caia"].probed_at == NOW


@pytest.mark.parametrize("verdict", ["limited", "error"])
def test_a_limited_or_error_answer_never_reports_cleared(
    tmp_path: Path, verdict: str
) -> None:
    rig = Rig(tmp_path, "h053", FakeProbe({"onexbot": answer(verdict)}))
    rig.limit("onexbot", STALE_STAMP)
    result = rig.tick()
    assert result.limits_cleared == ()
    got = {o.label: o for o in result.limit_rechecks}
    assert got["onexbot"].outcome == "kept"


def test_a_label_inside_the_interval_reports_not_due(tmp_path: Path) -> None:
    probe = FakeProbe({"onexbot": answer("limited")})
    rig = Rig(tmp_path, "h053", probe)
    rig.limit("onexbot", STALE_STAMP)
    first = rig.tick()
    rig.now = NOW + timedelta(seconds=INTERVAL - 1)
    second = rig.tick()
    assert probe.asked == ["onexbot"]
    got = {o.label: o for o in second.limit_rechecks}
    assert got["onexbot"].outcome == "not_due"
    assert got["onexbot"].probed_at == first.limit_rechecks[0].probed_at


def test_a_dry_run_reports_due_and_asks_nothing(tmp_path: Path) -> None:
    probe = FakeProbe({"onexbot": answer("ok")})
    rig = Rig(tmp_path, "h053", probe)
    rig.limit("onexbot", STALE_STAMP)
    result = rig.tick(dry_run=True)
    assert probe.asked == []
    got = {o.label: o for o in result.limit_rechecks}
    assert got["onexbot"].outcome == "due"


def test_not_configured_reports_no_outcomes(tmp_path: Path) -> None:
    rig = Rig(tmp_path, "h053", None, recheck=False)
    result = rig.tick()
    assert result.limit_recheck == "not_configured"
    assert result.limit_rechecks == ()


def test_the_printed_line_carries_the_typed_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(HOST_ENV, raising=False)
    monkeypatch.setenv(HOST_ENV, "h101")
    monkeypatch.setattr(
        f"{MODULE}.run_tick",
        lambda _host, **_kw: ModelClaudeTokenRotationResult(
            host="h101",
            rank=1,
            outcome="kept",
            in_use_state="usable",
            marks_sync=ModelRotationMarksSync(state="applied"),
            limit_rechecks=(
                ModelLimitRecheckOutcome(
                    label="onexbot", outcome="cleared", probed_at=NOW
                ),
                ModelLimitRecheckOutcome(label="caia", outcome="kept", probed_at=NOW),
            ),
        ),
    )
    state_dir = tmp_path / "state"
    code = main(["--state-dir", str(state_dir)])
    assert code == 0
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["limit_rechecks"] == [
        {"label": "onexbot", "outcome": "cleared", "probed_at": "2026-11-11T23:00:00Z"},
        {"label": "caia", "outcome": "kept", "probed_at": "2026-11-11T23:00:00Z"},
    ]
    # seam: the unit's heartbeat (last-run.json, the unit-state reader's source) carries the same typed outcomes
    beat = json.loads((state_dir / "last-run.json").read_text(encoding="utf-8"))
    assert beat["result"]["limit_rechecks"] == line["limit_rechecks"]
    assert beat["result"]["limit_recheck"] == "not_configured"


def test_an_error_chain_prints_status_and_no_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(HOST_ENV, "h101")

    def _raise(*_args, **_kwargs) -> ModelClaudeTokenRotationResult:
        raise ValueError("boom")

    monkeypatch.setattr(f"{MODULE}.run_tick", _raise)
    state_dir = tmp_path / "state"
    code = main(["--state-dir", str(state_dir)])
    assert code == 3
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["status"] == "overlay_refused"
    assert "limit_rechecks" not in line
