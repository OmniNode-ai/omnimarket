# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The command line of the sweep (OMN-24614): ``probe`` and ``sweep`` over the nodes' own synthetic overlays.

The three example overlays and the watcher state are copied into a temporary directory. The ledger
read command answers a typed STATUS query using a fixture reply. Exit 0 is a read and decided
sweep, 1 a source that could not be read (nothing posted), 3 a missing deployment fact.
"""

from __future__ import annotations

import json
import plistlib
import shlex
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_review_gate_context_orchestrator.runtime_review_gate_context_sweep import (
    main,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units import request
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
    ledger = tmp_path / "ledger-reply.json"
    stamp = datetime.now(UTC).strftime(STAMP)
    row = (
        f"{stamp} | STATUS | lane=orchestrator-31d9 | pr=example-repo#1 | "
        f"head={HEAD} | review=fable | verdict=CLEAN | reviewed"
    )
    ledger.write_text(
        json.dumps({"status": "ok", "rows": [{"raw_row": row}] if clean else []}),
        encoding="utf-8",
    )
    ledger_reader = (
        "import json, sys; from pathlib import Path; "
        "query = json.load(sys.stdin); "
        "assert query['query'] == 'rows' and query['kinds'] == ['STATUS'] "
        "and query['term'] == 'review='; "
        "print(Path(sys.argv[1]).read_text())"
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    monkeypatch.setenv("EXAMPLE_WATCHER_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv(
        "ONEX_LEDGER_BUS_READ_COMMAND",
        shlex.join([sys.executable, "-c", ledger_reader, str(ledger)]),
    )


def test_probe_reads_a_clean_head_as_success_and_posts_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _env(monkeypatch, tmp_path, clean=True, tick=datetime.now(UTC))
    assert main(["probe"]) == 0
    out = capsys.readouterr().out
    assert "example-org/example-repo#1 success clean_on_head not_posted" in out
    assert "posted=0" in out


def test_installer_tick_runs_the_overlay_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _env(monkeypatch, tmp_path, clean=True, tick=datetime.now(UTC))
    unit = load_units()["review-gate-context-probe"]
    installer_env = {
        "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays"),
        "ONEX_PR_WATCHER_STATE": str(tmp_path / "state.json"),
    }
    req = request(
        tmp_path, unit="review-gate-context-probe", path_overrides=installer_env
    )
    plist = plistlib.loads(
        HandlerRegistryUnits().render(req, unit, tmp_path, "uv", "python").encode()
    )
    assert plist["StartInterval"] == 300
    assert plist["RunAtLoad"]
    for name, value in installer_env.items():
        assert plist["EnvironmentVariables"][name] == value
    assert plist["EnvironmentVariables"]["ONEX_LEDGER_BUS_READ_COMMAND"] == (
        f"{req.registry_root}/.onex-dispatch-venv/bin/onex ledger query --stdin"
    )
    argv = plist["ProgramArguments"]
    module_index = argv.index("-m") + 1
    assert argv[module_index] == main.__module__
    assert main(argv[module_index + 1 :]) == 0
    assert "success clean_on_head not_posted" in capsys.readouterr().out
    evidence = Path(argv[argv.index("--state-dir") + 1]) / "last-run.json"
    result = json.loads(evidence.read_text(encoding="utf-8"))
    assert result["ok"]
    assert result["heads"] == 1
    assert result["posted"] == 0


def test_installer_tick_requires_an_overlay_schedule(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _env(monkeypatch, tmp_path, clean=True, tick=datetime.now(UTC))
    overlay = (
        tmp_path / "overlays" / "node_review_gate_context_orchestrator" / "overlay.yaml"
    )
    overlay.write_text(
        overlay.read_text(encoding="utf-8").split("\nschedule:", 1)[0],
        encoding="utf-8",
    )
    assert (
        main(["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "run")]) == 3
    )
    assert "overlay has no schedule" in capsys.readouterr().err


def test_installer_tick_refuses_a_file_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _env(monkeypatch, tmp_path, clean=True, tick=datetime.now(UTC))
    overlay = (
        tmp_path / "overlays" / "node_review_gate_context_orchestrator" / "overlay.yaml"
    )
    overlay.write_text(
        overlay.read_text(encoding="utf-8").replace(
            "route: command\n  command_env: ONEX_LEDGER_BUS_READ_COMMAND",
            "route: file\n  path_env: EXAMPLE_LEDGER_PATH",
        ),
        encoding="utf-8",
    )
    args = ["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "run")]
    assert main(args) == 3
    assert "requires a ledger read command" in capsys.readouterr().err


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


def test_a_watcher_state_that_is_not_an_object_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _env(monkeypatch, tmp_path, clean=True, tick=datetime.now(UTC))
    (tmp_path / "state.json").write_text("[1, 2]", encoding="utf-8")
    assert main(["probe"]) == 3
    assert "INDETERMINATE: ReviewGateSourceError" in capsys.readouterr().err
