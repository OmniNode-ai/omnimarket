# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The command line of the sweep (OMN-24614): ``probe`` and ``sweep`` over the nodes' own synthetic overlays.

The three example overlays are copied into a temporary overlay root and the watcher state and the ledger are files in the
temporary directory, so the run takes every setting from an overlay and every path from an environment variable. Exit 0 is a
read and decided sweep, 1 a source that could not be read (nothing posted), 3 a missing deployment fact.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_review_gate_context_orchestrator.__main__ import main
from tests.unit.nodes.node_review_gate_context_orchestrator.support import HEAD

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[4] / "src" / "omnimarket" / "nodes"
STAMP = "%Y-%m-%dT%H:%M:%SZ"


def _overlay_root(root: Path) -> None:
    for node in (
        "node_review_gate_context_compute",
        "node_review_gate_context_effect",
        "node_review_gate_context_orchestrator",
    ):
        (root / node).mkdir(parents=True)
        shutil.copy(NODES / node / "overlay.example.yaml", root / node / "overlay.yaml")


def _env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, clean: bool, tick: datetime
) -> None:
    root = tmp_path / "overlays"
    _overlay_root(root)
    state = {
        "last_tick": tick.strftime(STAMP),
        "prs": {
            "example-repo#1": {
                "facts": {
                    "repo": "example-repo",
                    "number": 1,
                    "head_sha": HEAD,
                    "author": "lane-author",
                    "head_ref": "omn-1-change",
                    "title": "OMN-1: a change",
                    "draft": False,
                    "state": "OPEN",
                }
            }
        },
    }
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    ledger = tmp_path / "ledger.md"
    stamp = datetime.now(UTC).strftime(STAMP)
    lines = []
    if clean:
        lines.append(
            f"{stamp} | STATUS | lane=orchestrator-31d9 | pr=example-repo#1 | head={HEAD} | review=fable | verdict=CLEAN | reviewed"
        )
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    monkeypatch.setenv("EXAMPLE_WATCHER_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("EXAMPLE_LEDGER_PATH", str(ledger))


def test_probe_reads_a_clean_head_as_success_and_posts_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _env(monkeypatch, tmp_path, clean=True, tick=datetime.now(UTC))
    assert main(["probe"]) == 0
    out = capsys.readouterr().out
    assert "example-org/example-repo#1 success clean_on_head not_posted" in out
    assert "posted=0" in out


def test_sweep_posts_red_for_a_head_with_no_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _env(monkeypatch, tmp_path, clean=False, tick=datetime.now(UTC))
    assert main(["sweep"]) == 0
    out = capsys.readouterr().out
    assert "example-org/example-repo#1 failure review_missing posted" in out
    assert "posted=1" in out


def test_a_stale_watcher_state_reads_nothing_and_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _env(monkeypatch, tmp_path, clean=True, tick=datetime(2026, 1, 1, tzinfo=UTC))
    assert main(["sweep"]) == 1
    captured = capsys.readouterr()
    assert "past the" in captured.err
    assert "posted=0" in captured.out


def test_a_missing_overlay_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "none"))
    assert main(["probe"]) == 3
    assert "INDETERMINATE" in capsys.readouterr().err
