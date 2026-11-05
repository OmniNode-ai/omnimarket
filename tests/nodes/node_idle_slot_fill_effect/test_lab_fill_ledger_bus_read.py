# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The recorded 274,481-line read must use the declared query command."""

from __future__ import annotations

import importlib
import json
import shlex
import sys
from pathlib import Path
from typing import get_type_hints

import pytest
import yaml

from omnimarket.nodes.node_idle_slot_fill_effect.handlers import (
    handler_idle_slot_fill as fill,
)

NOW = "2026-11-04T23:00:00Z"
CLAIM = "2026-11-04T22:50:00Z | CLAIM | lane=probe-owner | ticket=OMN-19998 | actor=codex | worktree=none | scope=probe"


@pytest.fixture
def recorded_read(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger.write_text("# recorded size fixture\n" * 274480 + CLAIM + "\n")
    plugin = tmp_path / "plugin"
    scripts = plugin / "skills/merge-drain/scripts"
    scripts.mkdir(parents=True)
    (scripts / "drain_map.py").write_text("""
from dataclasses import dataclass
@dataclass
class Row:
    n: int
    ts: str
    rtype: str
    lane: str
    text: str
    source: str

def load_grammar(root):
    return None

def load_rows(root, days, now, grammar, ledger):
    line = ledger.read_bytes().decode().splitlines()[-1]
    return [Row(0, line[:20], 'CLAIM', 'probe-owner', line, 'file:274481')], []

def open_claims(rows, grammar, horizon):
    return [r for r in rows if r.rtype == 'CLAIM']
""")
    marker = tmp_path / "query.json"
    publisher = tmp_path / "publisher.py"
    publisher.write_text("""
import json
import sys
from pathlib import Path
request = json.load(sys.stdin)
Path(sys.argv[1]).write_text(json.dumps(request))
print(json.dumps({
    'correlation_id': request['correlation_id'], 'query': 'rows',
    'window_since': request.get('since'), 'window_until': request.get('until'),
    'matched': 1, 'rows': [{
        'row_id': 'probe-claim', 'row_ts': '2026-11-04T22:50:00Z',
        'row_type': 'CLAIM', 'row_lane': 'probe-owner',
        'text': sys.argv[2], 'raw_row': sys.argv[2], 'source': 'recorded-case'
    }]
}))
""")
    env = {
        "OMNI_HOME": str(tmp_path),
        "CLAUDE_PLUGIN_ROOT": str(plugin),
        "ONEX_LEDGER_PATH": str(ledger),
        "ONEX_LEDGER_BUS_READ_COMMAND": shlex.join(
            [sys.executable, str(publisher), str(marker), CLAIM]
        ),
    }
    return env, ledger, marker


def test_configured_command_replaces_the_recorded_file_read(recorded_read, monkeypatch):
    env, ledger, marker = recorded_read
    reads = []
    original = Path.read_bytes

    def counted(path):
        if path == ledger:
            reads.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", counted)
    sources = fill.default_sources(NOW, env)
    assert sources is not None
    assert sources.ledger_tickets is not None
    claimed, readbacks = sources.ledger_tickets()
    assert claimed == {"OMN-19998"}
    assert readbacks == {}
    assert reads == [], "a configured query command must bypass the 274,481-line ledger"
    request = json.loads(marker.read_text())
    assert request["query"] == "rows"
    assert request["since"] == "2026-11-02T23:00:00Z"
    assert request["until"] == NOW


def test_unconfigured_reader_keeps_the_file_positive_control(recorded_read):
    env, _, marker = recorded_read
    env.pop("ONEX_LEDGER_BUS_READ_COMMAND")
    sources = fill.default_sources(NOW, env)
    assert sources is not None
    assert sources.ledger_tickets is not None
    assert sources.ledger_tickets() == ({"OMN-19998"}, {})
    assert not marker.exists()


@pytest.mark.parametrize(
    "mode",
    ["exit", "timeout", "json", "correlation", "query", "error", "truncated", "cursor"],
)
def test_failed_query_never_falls_back_to_the_large_file(
    recorded_read, monkeypatch, mode
):
    env, ledger, _ = recorded_read
    argv = shlex.split(env["ONEX_LEDGER_BUS_READ_COMMAND"])
    publisher = Path(argv[1])
    source = publisher.read_text()
    if mode == "exit":
        source = "import sys; sys.exit(17)"
    elif mode == "timeout":
        source = "import time; time.sleep(5)"
        env["ONEX_LEDGER_BUS_TIMEOUT_S"] = "0.05"
    elif mode == "json":
        source = "print('not json')"
    elif mode == "correlation":
        source = source.replace(
            "request['correlation_id']", "'00000000-0000-0000-0000-000000000000'"
        )
    elif mode == "query":
        source = source.replace("'query': 'rows'", "'query': 'holds'")
    elif mode == "error":
        source = source.replace("'matched': 1", "'error': 'unread', 'matched': 1")
    elif mode == "truncated":
        source = source.replace("'matched': 1", "'matched': 2")
    else:
        source = source.replace("'matched': 1", "'next_cursor': 'more', 'matched': 1")
    publisher.write_text(source)
    original = Path.read_bytes

    def refuse_file(path):
        if path == ledger:
            pytest.fail("failed query fell back to ledger file")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", refuse_file)
    sources = fill.default_sources(NOW, env)
    assert sources is not None
    assert sources.ledger_tickets is not None
    with pytest.raises(ValueError, match="lab-fill ledger query"):
        sources.ledger_tickets()


def test_overlay_can_supply_the_query_command(recorded_read, tmp_path):
    env, _, marker = recorded_read
    command = env.pop("ONEX_LEDGER_BUS_READ_COMMAND")
    overlay = tmp_path / "overlays/idle_slot_fill/overlay.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text(
        json.dumps({"ledger_read_command": command, "ledger_read_timeout_s": 3})
    )
    env["ONEX_SKILL_OVERLAY_ROOTS"] = str(overlay.parent.parent)
    sources = fill.default_sources(NOW, env)
    assert sources is not None
    assert sources.ledger_tickets is not None
    assert sources.ledger_tickets() == ({"OMN-19998"}, {})
    assert marker.exists()


@pytest.mark.parametrize("setting", ["ONEX_LEDGER_FILE_RETIRED", "ONEX_LEDGER_RECORD"])
def test_bus_record_without_command_refuses_file_read(recorded_read, setting):
    env, _, _ = recorded_read
    env.pop("ONEX_LEDGER_BUS_READ_COMMAND")
    env[setting] = "1" if setting.endswith("RETIRED") else "bus"
    sources = fill.default_sources(NOW, env)
    assert sources is not None
    assert sources.ledger_tickets is not None
    with pytest.raises(ValueError, match="ONEX_LEDGER_BUS_READ_COMMAND"):
        sources.ledger_tickets()


def test_contract_routes_the_typed_ledger_request(recorded_read):
    env, ledger, marker = recorded_read
    root = Path(fill.__file__).parents[1]
    contract = yaml.safe_load((root / "contract.yaml").read_text())
    route = next(
        row
        for row in contract["handler_routing"]["handlers"]
        if row["operation"] == "read-ledger"
    )
    handler_class = getattr(
        importlib.import_module(route["handler"]["module"]), route["handler"]["name"]
    )

    def model(reference):
        module, _, name = reference.rpartition(".")
        return getattr(importlib.import_module(module), name)

    request_class = model(route["input_model"])
    result_class = model(route["output_model"])
    hints = get_type_hints(handler_class.handle)
    assert hints["request"] is request_class
    assert hints["return"] is result_class
    result = handler_class(env).handle(
        request_class(
            now=NOW,
            ledger=ledger,
            drain_map=Path(env["CLAUDE_PLUGIN_ROOT"])
            / "skills/merge-drain/scripts/drain_map.py",
            registry_root=Path(env["OMNI_HOME"]),
        )
    )
    assert isinstance(result, result_class)
    assert result.claimed == frozenset({"OMN-19998"})
    assert result.readbacks == {}
    assert marker.exists()


def test_malformed_selected_overlay_does_not_become_unconfigured(
    recorded_read, tmp_path
):
    env, _, marker = recorded_read
    overlay = tmp_path / "overlays/idle_slot_fill/overlay.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text(json.dumps({"ledger_read_timeout_s": 0}))
    env["ONEX_SKILL_OVERLAY_ROOTS"] = str(overlay.parent.parent)
    sources = fill.default_sources(NOW, env)
    assert sources is not None
    assert sources.ledger_tickets is not None
    with pytest.raises(ValueError, match="ledger_read_timeout_s"):
        sources.ledger_tickets()
    assert not marker.exists()
