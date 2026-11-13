# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain of node_host_retention_effect through its node entry point and its command line."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_host_retention_effect import runtime_host_retention
from omnimarket.nodes.node_host_retention_effect.handlers.handler_host_retention import (
    HandlerHostRetention,
)
from omnimarket.nodes.node_host_retention_effect.models.model_host_retention import (
    ModelHostRetentionRequest,
)
from omnimarket.nodes.node_host_retention_effect.node import NodeHostRetentionEffect
from tests.nodes.node_host_retention_effect.host_retention_fakes import (
    NOW,
    FakeLanes,
    entry,
    make_ports,
    overlay_file,
    scratch_class,
)

pytestmark = pytest.mark.unit


def node_with(ports_lanes: set[str]) -> NodeHostRetentionEffect:
    return NodeHostRetentionEffect(
        now=lambda: NOW, ports=make_ports(lanes=FakeLanes(ports_lanes))
    )


def test_golden_chain_removes_the_old_entry_and_keeps_the_live_lane_one(
    tmp_path: Path,
) -> None:
    root = tmp_path / "scratch"
    root.mkdir()
    old = entry(root, "finished-session", days=9)
    live = entry(root, "running-lane-z-0001", days=9)
    node = node_with({"lane-z"})
    contract = yaml.safe_load(node.contract_path.read_text())
    assert contract["name"] == "node_host_retention_effect"
    assert (
        contract["terminal_event"] == "onex.evt.omnimarket.host-retention-completed.v1"
    )
    assert contract["runtime_dispatch"]["terminal_events"] == {
        "success": "onex.evt.omnimarket.host-retention-completed.v1",
        "failure": "onex.evt.omnimarket.host-retention-failed.v1",
    }

    result = node.handle(
        ModelHostRetentionRequest(
            host_name="pool-a",
            overlay_file=overlay_file(tmp_path, age_classes=[scratch_class(root)]),
        )
    )

    assert result.status == "ok"
    assert [Path(i.path).name for i in result.items] == ["finished-session"]
    assert not old.exists()
    assert live.exists()
    assert [(Path(k.path).name, k.reason) for k in result.kept] == [
        ("running-lane-z-0001", "live_lane")
    ]
    again = node.handle(
        ModelHostRetentionRequest(
            host_name="pool-a",
            overlay_file=overlay_file(tmp_path, age_classes=[scratch_class(root)]),
        )
    )
    assert again.items == ()


def test_error_chain_a_root_the_contract_does_not_allow_is_refused_and_touches_nothing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "elsewhere"
    root.mkdir()
    old = entry(root, "old", days=30)

    result = node_with(set()).handle(
        ModelHostRetentionRequest(
            host_name="pool-a",
            overlay_file=overlay_file(tmp_path, age_classes=[scratch_class(root)]),
        )
    )

    assert result.status == "refused"
    assert "the contract allows only" in result.refusals[0]
    assert result.items == ()
    assert result.disk_before == ()
    assert old.exists()


def test_command_line_exit_codes_and_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "scratch"
    root.mkdir()
    old = entry(root, "old", days=9)
    monkeypatch.setattr(
        runtime_host_retention,
        "HandlerHostRetention",
        lambda: HandlerHostRetention(now=lambda: NOW, ports=make_ports()),
    )
    good = overlay_file(tmp_path, age_classes=[scratch_class(root)])

    assert (
        runtime_host_retention.main(
            ["run", "--host", "pool-a", "--overlay-file", good, "--dry-run"]
        )
        == 0
    )
    printed = json.loads(capsys.readouterr().out)
    assert printed["dry_run"] is True
    assert [Path(i["path"]).name for i in printed["items"]] == ["old"]
    assert old.exists()

    assert (
        runtime_host_retention.main(
            [
                "run",
                "--host",
                "pool-a",
                "--overlay-file",
                good,
                "--class",
                "runner_work",
            ]
        )
        == 0
    )
    assert old.exists(), "a class filter that names no declared class removes nothing"
    capsys.readouterr()

    assert (
        runtime_host_retention.main(["run", "--host", "pool-z", "--overlay-file", good])
        == 2
    )
    assert json.loads(capsys.readouterr().out)["status"] == "refused"
    assert runtime_host_retention.main(["run", "--nonsense"]) == 3
    assert old.exists()

    assert (
        runtime_host_retention.main(["run", "--host", "pool-a", "--overlay-file", good])
        == 0
    )
    assert not old.exists()
