# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The installed replay verb runs the node through the declared in-memory bus."""

from importlib.metadata import entry_points
from pathlib import Path

from click.testing import CliRunner
from omnibase_core.cli.cli_run_node import _resolve_node_topics, run_node_in_process

from omnimarket.models.error_rule_match import ModelErrorRuleMatchRequest
from omnimarket.nodes.node_error_rule_replay_compute.cli import replay_command
from tests.unit.nodes.node_error_rule_match_compute.test_match import normalize, table
from tests.unit.nodes.node_error_rule_match_compute.test_replay import corpus_request


def test_replay_command_prints_two_identical_corpus_reports(tmp_path, monkeypatch):
    root = tmp_path / "config/onex/overlays/node_error_rule_replay_compute"
    root.mkdir(parents=True)
    (root / "overlay.yaml").write_text("event_bus: {type: inmemory}\n")
    monkeypatch.setenv("ONEX_WORKSPACE_CONFIG_ROOT", str(tmp_path))
    monkeypatch.setenv("OMNIBASE_PATH", str(tmp_path))
    request = tmp_path / "request.json"
    request.write_text(corpus_request().model_dump_json())
    args = ["--input-file", str(request)]
    first = CliRunner().invoke(replay_command, args)
    second = CliRunner().invoke(replay_command, args)
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert first.output == second.output
    assert (
        "hits=0 misses=4184 false_matches=0 labelled=0 unlabelled=4184" in first.output
    )
    assert "NO-MATCH " in first.output
    (entry,) = entry_points(group="onex.cli", name="error-rule-replay")
    assert entry.load() is replay_command


def test_run_node_entry_resolves_and_hosts_match_contract():
    node = "node_error_rule_match_compute"
    contract, command, terminal, inject = _resolve_node_topics(node)
    assert command
    assert terminal
    assert inject is False
    request = ModelErrorRuleMatchRequest(
        normalized=normalize("uncovered cause"), table=table()
    )
    result = run_node_in_process(
        node, Path(contract), request.model_dump(mode="json"), 30
    )
    assert result["decision"]["status"] == "NO-MATCH"
