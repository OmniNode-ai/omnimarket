# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The declared PR watcher refresh command, shown failing without it (OMN-23033).

The control: a planted stale watcher state, with the bus holding a newer pull request event,
reads ``mirror_behind`` when the host declares no refresh command and when the declared tick
changes nothing. After the real command (``repo-state-refresh``, run as a subprocess the way
the query's puller runs it) the same query reads current.
"""

from __future__ import annotations

import json
import shlex
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnimarket.nodes.node_repo_state_query_effect.handlers.adapter_repo_state_mirrors import (
    CommandMirrorPuller,
)
from omnimarket.nodes.node_repo_state_query_effect.handlers.handler_repo_state_query import (
    HandlerRepoStateQuery,
)
from omnimarket.nodes.node_repo_state_query_effect.models.model_repo_state_query import (
    EnumRepoStateSource,
    EnumRepoStateWant,
    ModelRepoStateQuery,
)
from omnimarket.nodes.node_repo_state_watcher_refresh_effect.handlers.adapter_watcher_refresh_cli import (
    watcher_refresh_command,
)
from tests.nodes.node_repo_state_query_effect.planted import HOST, REPO, Planted, plant

CLI_MODULE = "omnimarket.nodes.node_repo_state_watcher_refresh_effect.handlers.adapter_watcher_refresh_cli"
STALE = "2026-11-07T21:00:00Z"

TICK_WRITES = textwrap.dedent(
    """
    import json, sys
    from datetime import UTC, datetime
    path = sys.argv[1]
    data = json.load(open(path))
    data["last_tick"] = datetime.now(UTC).isoformat()
    json.dump(data, open(path, "w"))
    """
)
TICK_WRITES_NOTHING = "import sys\n"
TICK_FAILS = "import sys\nsys.exit(3)\n"


def _declare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: Path, tick_body: str
) -> Path:
    """The host's declaration: an overlay naming env variables, and the env variables."""
    tick = tmp_path / "tick.py"
    tick.write_text(tick_body, encoding="utf-8")
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        textwrap.dedent(
            """
            watcher_state_env: PLANTED_WATCHER_STATE
            tick_command_env: PLANTED_WATCHER_TICK
            state_sync_command_env: null
            step_timeout_seconds: 30
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PLANTED_WATCHER_STATE", str(state))
    monkeypatch.setenv(
        "PLANTED_WATCHER_TICK",
        shlex.join([sys.executable, str(tick), str(state)]),
    )
    return overlay


def _handler(p: Planted, argv: list[str] | None) -> HandlerRepoStateQuery:
    """The planted handler, its watcher puller replaced by the one a host would declare."""
    h = p.handler
    return HandlerRepoStateQuery(
        host_name=HOST,
        clone=h._clone,
        watcher=h._watcher,
        bus_state=h._bus,
        puller=CommandMirrorPuller(
            h._puller._clone_refresh,
            watcher_argv=argv,
            timeout_seconds=60,
        ),
    )


def _pr_query() -> ModelRepoStateQuery:
    return ModelRepoStateQuery(
        host=HOST, repo=REPO, want=EnumRepoStateWant.PULL_REQUEST, pr_number=7
    )


def _command(overlay: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        CLI_MODULE,
        "--repo",
        "{repo}",
        "--overlay",
        str(overlay),
    ]


def _planted_stale(tmp_path: Path) -> Planted:
    p = plant(tmp_path, watcher_tick=STALE)
    p.bus_merge("b" * 40, pr=7, at=datetime.now(UTC))
    return p


async def test_control_no_declared_refresh_command_reads_mirror_behind(
    tmp_path: Path,
) -> None:
    p = _planted_stale(tmp_path)
    answer = await _handler(p, None).handle(_pr_query())
    assert answer.source is EnumRepoStateSource.MIRROR_BEHIND
    assert answer.current is False
    assert answer.pull is not None
    assert answer.pull.status == "not_configured"


async def test_control_a_tick_that_changes_nothing_still_reads_mirror_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _planted_stale(tmp_path)
    overlay = _declare(tmp_path, monkeypatch, p.watcher_path, TICK_WRITES_NOTHING)
    answer = await _handler(p, _command(overlay)).handle(_pr_query())
    assert answer.source is EnumRepoStateSource.MIRROR_BEHIND
    assert answer.current is False
    assert answer.pull is not None
    assert not answer.pull.ok
    assert "unchanged" in answer.pull.message


async def test_golden_the_declared_command_makes_the_stale_watcher_read_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _planted_stale(tmp_path)
    overlay = _declare(tmp_path, monkeypatch, p.watcher_path, TICK_WRITES)
    answer = await _handler(p, _command(overlay)).handle(_pr_query())
    assert answer.ok
    assert answer.source is EnumRepoStateSource.MIRROR_AFTER_PULL
    assert answer.current is True
    assert answer.pull is not None
    assert answer.pull.ok
    assert answer.pull.status == "refreshed"
    assert json.loads(p.watcher_path.read_text())["last_tick"] != STALE


def test_golden_chain_cli_refreshed_exit_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = plant(tmp_path, watcher_tick=STALE)
    overlay = _declare(tmp_path, monkeypatch, p.watcher_path, TICK_WRITES)
    result = CliRunner().invoke(
        watcher_refresh_command, ["--repo", REPO, "--overlay", str(overlay)]
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["status"] == "refreshed"
    assert body["ok"] is True
    assert body["read_at_after"] > body["read_at_before"]
    assert [s["name"] for s in body["steps"]] == ["tick"]


@pytest.mark.parametrize(
    ("tick_body", "status", "exit_code"),
    [(TICK_WRITES_NOTHING, "unchanged", 1), (TICK_FAILS, "failed", 1)],
)
def test_error_chain_a_tick_that_does_not_move_the_state_is_not_ok(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tick_body: str,
    status: str,
    exit_code: int,
) -> None:
    p = plant(tmp_path, watcher_tick=STALE)
    overlay = _declare(tmp_path, monkeypatch, p.watcher_path, tick_body)
    result = CliRunner().invoke(
        watcher_refresh_command, ["--repo", REPO, "--overlay", str(overlay)]
    )
    assert result.exit_code == exit_code
    body = json.loads(result.output)
    assert body["status"] == status
    assert body["ok"] is False


def test_error_chain_an_undeclared_tick_command_refuses_before_running_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = plant(tmp_path, watcher_tick=STALE)
    overlay = _declare(tmp_path, monkeypatch, p.watcher_path, TICK_WRITES)
    monkeypatch.delenv("PLANTED_WATCHER_TICK")
    result = CliRunner().invoke(
        watcher_refresh_command, ["--repo", REPO, "--overlay", str(overlay)]
    )
    assert result.exit_code == 2
    assert "PLANTED_WATCHER_TICK" in result.output
