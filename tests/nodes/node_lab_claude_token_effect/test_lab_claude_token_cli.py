# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex lab claude-token`` (OMN-19998): the token comes from stdin only and is never
echoed; hosts resolve from the lab host table; the contract and entry point are wired.
Runs against a fake host and a fake store; no real token or host is touched."""

from __future__ import annotations

import importlib
import json
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from omnimarket.nodes.node_lab_claude_token_effect import cli
from omnimarket.nodes.node_lab_claude_token_effect.handlers.handler_lab_claude_token import (
    PROBE_SCRIPT,
    HandlerLabClaudeToken,
    active_ref,
    pool_index_ref,
    pool_ref,
)
from omnimarket.nodes.node_lab_claude_token_effect.handlers.handler_lab_host_shell import (
    host_argv,
)
from omnimarket.nodes.node_lab_claude_token_effect.models.model_lab_claude_token_request import (
    ModelLabTokenHost,
)
from tests.nodes.node_lab_claude_token_effect.test_lab_claude_token_effect import (
    FakeHost,
    FakeStore,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODE = REPO / "src" / "omnimarket" / "nodes" / "node_lab_claude_token_effect"
FAKE = "fake-cli-token-4444"
TABLE = """\
hosts:
  - name: h200
    target: launching-host
    local: true
  - name: h202
    target: lab-h202
"""


@pytest.fixture
def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, FakeStore, FakeHost]:
    table = tmp_path / "lab_run_hosts.yaml"
    table.write_text(TABLE)
    placement = tmp_path / "placement"
    placement.mkdir()
    monkeypatch.setenv("ONEX_LAB_RUN_HOSTS", str(table))
    monkeypatch.setenv("LANDING_PLACEMENT_DIR", str(placement))
    store, host = FakeStore(), FakeHost(None, {FAKE: (0, "ok")})
    monkeypatch.setattr(
        cli, "build_handler", lambda: HandlerLabClaudeToken(store=store, shell=host)
    )
    return placement, store, host


def test_set_reads_stdin_and_never_echoes_it(
    env: tuple[Path, FakeStore, FakeHost],
) -> None:
    placement, store, host = env
    until = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (placement / ".limited-h202").write_text(
        json.dumps({"host": "h202", "until": until})
    )
    out = CliRunner().invoke(
        cli.lab_group,
        ["claude-token", "set", "--host", "h202", "--account", "acct1"],
        input=FAKE + "\n",
    )
    assert out.exit_code == 0, out.output
    assert FAKE not in out.output
    assert "lab.claude_token.h202.acct1" in out.output
    assert host.current == FAKE
    assert store.values[active_ref("h202")] == "acct1"
    assert not (placement / ".limited-h202").exists()


def test_set_has_no_token_argument(env: tuple[Path, FakeStore, FakeHost]) -> None:
    out = CliRunner().invoke(
        cli.lab_group,
        ["claude-token", "set", "--host", "h202", "--account", "a", "--token", FAKE],
    )
    assert out.exit_code != 0
    assert "No such option" in out.output


def test_set_with_empty_stdin_is_refused(env: tuple[Path, FakeStore, FakeHost]) -> None:
    _, _, host = env
    out = CliRunner().invoke(
        cli.lab_group,
        ["claude-token", "set", "--host", "h202", "--account", "a"],
        input="",
    )
    assert out.exit_code != 0
    assert "stdin only" in out.output
    assert host.calls == []


def test_set_failed_probe_exits_nonzero(env: tuple[Path, FakeStore, FakeHost]) -> None:
    out = CliRunner().invoke(
        cli.lab_group,
        ["claude-token", "set", "--host", "h202", "--account", "a"],
        input="fake-unanswered-5555",
    )
    assert out.exit_code == 1
    assert "restored" in out.output
    assert "fake-unanswered-5555" not in out.output


def test_unknown_host_is_refused(env: tuple[Path, FakeStore, FakeHost]) -> None:
    out = CliRunner().invoke(
        cli.lab_group,
        ["claude-token", "set", "--host", "h999", "--account", "a"],
        input=FAKE,
    )
    assert out.exit_code != 0
    assert "not in the lab host table" in out.output


def test_dry_run_against_h202_prints_references_only(
    env: tuple[Path, FakeStore, FakeHost],
) -> None:
    _, store, host = env
    out = CliRunner().invoke(
        cli.lab_group,
        ["claude-token", "set", "--host", "h202", "--account", "acct1", "--dry-run"],
        input=FAKE,
    )
    assert out.exit_code == 0, out.output
    assert "lab.claude_token.h202.acct1" in out.output
    assert "ssh lab-h202" in out.output
    assert FAKE not in out.output
    assert host.calls == []
    assert store.values == {}


def test_rotate_limited_walks_every_marker(
    env: tuple[Path, FakeStore, FakeHost],
) -> None:
    placement, store, _host = env
    store.values.update(
        {
            pool_ref("spare-a"): FAKE,
            pool_index_ref(): "spare-a",
            active_ref("h202"): "acct1",
        }
    )
    until = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (placement / ".limited-h202").write_text(
        json.dumps({"host": "h202", "until": until})
    )
    out = CliRunner().invoke(cli.lab_group, ["claude-token", "rotate-limited"])
    assert out.exit_code == 0, out.output
    assert "rotated from acct1 to spare-a" in out.output
    assert "event: " in out.output
    assert FAKE not in out.output


def test_add_spare_reads_stdin(env: tuple[Path, FakeStore, FakeHost]) -> None:
    _, store, _ = env
    out = CliRunner().invoke(
        cli.lab_group, ["claude-token", "add-spare", "--account", "spare-z"], input=FAKE
    )
    assert out.exit_code == 0
    assert FAKE not in out.output
    assert store.values[pool_ref("spare-z")] == FAKE


def test_ssh_argv_carries_the_script_never_a_token() -> None:
    argv = host_argv(ModelLabTokenHost(name="h202", target="lab-h202"), PROBE_SCRIPT)
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv
    assert argv[-2:] == ["lab-h202", PROBE_SCRIPT]
    local = host_argv(ModelLabTokenHost(name="h200", local=True), PROBE_SCRIPT)
    assert local[0] == "/usr/bin/env"
    assert local[-1] == PROBE_SCRIPT


def test_contract_names_the_handler_and_declares_no_publish() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    module = importlib.import_module(contract["handler"]["module"])
    assert getattr(module, contract["handler"]["class"]) is HandlerLabClaudeToken
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    entry = tomllib.loads((REPO / "pyproject.toml").read_text())["project"][
        "entry-points"
    ]["onex.cli"]
    assert entry["lab"] == "omnimarket.nodes.node_lab_claude_token_effect.cli:lab_group"


def test_no_lab_host_table_is_refused(
    env: tuple[Path, FakeStore, FakeHost], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_LAB_RUN_HOSTS")
    out = CliRunner().invoke(
        cli.lab_group,
        ["claude-token", "set", "--host", "h202", "--account", "a"],
        input=FAKE,
    )
    assert out.exit_code != 0
    assert "set ONEX_LAB_RUN_HOSTS" in out.output
