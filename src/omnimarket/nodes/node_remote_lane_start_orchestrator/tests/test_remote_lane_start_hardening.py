# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22848: a close that did not land is not final, a repo needs its sha, the timeout bounds the loop."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_scripted_transport import (
    ModelScriptedTransportScript,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.models.model_remote_lane_start import (
    EnumRemoteLaneStartOutcome,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.runtime_remote_lane_start import (
    EXIT_USAGE,
    main,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.lane_start_fixtures import (
    DONE_MESSAGE,
    T0,
    Rig,
    cells,
    request,
    settings,
    stream_result,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols.local_ledger_append_command import (
    ModelAppendCommandResult,
)

pytestmark = [pytest.mark.unit]

DONE = ModelScriptedTransportScript(
    exit_polls=1, log_lines=("starting", stream_result(DONE_MESSAGE))
)


def test_a_refused_terminal_is_not_a_durable_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, DONE)
    appended = rig.executor.append

    def refuse_terminal(rows: str) -> ModelAppendCommandResult:
        if rows.split(" | ")[1] == "TERMINAL":
            return ModelAppendCommandResult(exit_code=65, stderr="row refused")
        return appended(rows)

    monkeypatch.setattr(rig.executor, "append", refuse_terminal)
    result = rig.orchestrator.run(request())
    assert rig.ledger_rows("TERMINAL") == []
    assert result.exit_code != 0
    assert not result.receipt.final
    assert "not persisted" in (result.receipt.error or "")
    assert result.receipt.status == "ledger-refused"


def test_a_repo_request_without_a_sha_is_refused_before_the_claim(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, DONE)
    result = rig.orchestrator.run(request(repo="omnimarket"))
    assert result.outcome is EnumRemoteLaneStartOutcome.UNSUPPORTED_SHAPE
    assert result.exit_code == 64
    assert rig.ledger_rows() == []
    assert rig.host_calls() == []
    assert any("without a sha" in line for line in result.lines)


def _ticking_rig(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: ModelScriptedTransportScript,
) -> Rig:
    """A rig whose clock moves 30 s with every sleep of the supervision loop."""
    rig = Rig(tmp_path, script)
    now = [T0]

    def sleep(seconds: float) -> None:
        now[0] += timedelta(seconds=30)

    monkeypatch.setattr(rig.orchestrator, "_clock", lambda: now[0])
    monkeypatch.setattr(rig.orchestrator, "_sleep", sleep)
    return rig


def test_timeout_min_bounds_the_supervision_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _ticking_rig(
        tmp_path, monkeypatch, ModelScriptedTransportScript(exit_polls=2000)
    )
    result = rig.orchestrator.run(request(timeout_min=1))
    assert "host:stop_engine host=h106" in rig.calls
    assert result.outcome is EnumRemoteLaneStartOutcome.STOPPED
    polls = [c for c in rig.calls if c.startswith("host:read_engine_exit")]
    assert len(polls) < 10


def test_an_engine_that_survives_the_timeout_stop_leaves_the_claim_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _ticking_rig(
        tmp_path,
        monkeypatch,
        ModelScriptedTransportScript(exit_polls=2000, stop_confirmed=False),
    )
    result = rig.orchestrator.run(request(timeout_min=1))
    assert result.outcome is EnumRemoteLaneStartOutcome.STOP_REFUSED
    assert rig.ledger_rows("TERMINAL") == []
    assert not result.receipt.final


def _overlay_with(tmp_path: Path, **section: object) -> Path:
    base = yaml.safe_load(
        (Path(__file__).parents[1] / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    base["remote_lane_start"].update(section)
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump(base), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("key", "value"),
    [
        # a literal brace (a json literal in an engine command) is a format field the request cannot fill
        ("engine_argv", ["sh", "-c", 'echo {"type":"result"}']),
        # a placeholder the request does not carry
        ("engine_argv", ["claude", "-p", "{brief}", "--effort", "{effort}"]),
        ("claim_text", "remote lane on {host} at {when}"),
    ],
)
def test_an_unfillable_placeholder_is_refused_at_load_naming_the_key(
    key: str, value: object
) -> None:
    with pytest.raises(ValueError, match=rf"remote_lane_start\.{key}"):
        settings(**{key: value})


def test_an_unfillable_placeholder_stops_the_entry_before_any_claim(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text("", encoding="utf-8")
    brief = tmp_path / "brief.md"
    brief.write_text("print the host name and stop", encoding="utf-8")
    pool = tmp_path / "pool.json"
    pool.write_text('{"readings": []}', encoding="utf-8")
    overlay = _overlay_with(
        tmp_path,
        engine_argv=["sh", "-c", 'echo {"type":"x"}'],
        receipt_dir=str(tmp_path / "receipts"),
    )
    code = main(
        [
            "--overlay", str(overlay), "--ledger", str(ledger),
            "--pool-snapshot", str(pool), "--lane", "fixture-lane",
            "--ticket", "OMN-22848", "--model", "sonnet",
            "--brief", str(brief), "--run-id", "rlane-fixture-1",
        ]
    )  # fmt: skip
    assert code == EXIT_USAGE
    assert ledger.read_text(encoding="utf-8") == ""


def test_the_claim_carries_the_worktree_cell_the_ledger_grammar_requires(
    tmp_path: Path,
) -> None:
    """The live ledger refused the first real CLAIM of this entry: no worktree cell (OMN-20148)."""
    rig = Rig(tmp_path, DONE)
    rig.orchestrator.run(request())
    [claim] = rig.attempted_rows("CLAIM")
    assert cells(claim)["worktree"] == "none"
    # No TERMINAL for a lane with no checkout names a worktree outcome either.
    [terminal] = rig.attempted_rows("TERMINAL")
    assert "worktree" not in cells(terminal)


def test_the_claim_of_a_repo_lane_names_its_checkout_and_the_terminal_keeps_it(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, DONE)
    rig.orchestrator.run(request(repo="omnimarket", sha="a" * 40))
    [claim] = rig.attempted_rows("CLAIM")
    path = cells(claim)["worktree"]
    assert path != "none"
    assert path.endswith("/omnimarket")
    [terminal] = rig.attempted_rows("TERMINAL")
    assert cells(terminal)["worktree"].startswith(f"kept:{path}:")
