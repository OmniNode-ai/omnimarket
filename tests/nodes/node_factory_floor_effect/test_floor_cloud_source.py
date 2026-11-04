# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regressions for the published floor's cloud source and stopped-line evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.cloud_session_placement import ModelCloudSessionPlacementRequest
from omnimarket.models.model_factory_floor_input import (
    ModelFloorManifestTask,
    ModelFloorTableTask,
)
from omnimarket.nodes.node_cloud_session_placement_effect.handlers import (
    HandlerCloudSessionPlacement,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    compute_factory_floor,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    _with_table_evidence,
)
from tests.nodes.node_factory_floor_compute.support import (
    pr,
    request,
    row,
    table_plan,
    wave_plan,
)

pytestmark = pytest.mark.unit


def test_release_note_ticket_mentions_do_not_stop_a_table_line() -> None:
    plan = table_plan()
    task = plan.table_tasks[1].model_copy(
        update={"prs": ("memory#713",), "state_basis": "watcher-open-pr"}
    )
    plan = plan.model_copy(update={"table_tasks": (task,)})
    unrelated = pr("memory#713", title="build(deps): bump actions", red=True)
    line = compute_factory_floor(request(plans=(plan,), prs=(unrelated,))).lines[0]
    assert line.condition != "stopped"
    assert line.blocker is None
    assert line.stations[0].items[0].prs == ()
    linked = unrelated.model_copy(update={"title": "fix OMN-8: repair the task"})
    line = compute_factory_floor(request(plans=(plan,), prs=(linked,))).lines[0]
    assert line.condition == "stopped"
    assert line.blocker is not None
    assert "memory#713" in line.blocker.reason


def test_inferred_table_pr_does_not_become_an_explicit_prose_task_link() -> None:
    task = ModelFloorManifestTask(id="T0.2", tickets=("OMN-100",), scoped=True)
    table = ModelFloorTableTask(
        label="T0.2",
        tickets=("OMN-100",),
        prs=("memory#713",),
        state="open-red",
        state_basis="watcher-open-pr",
    )
    assert _with_table_evidence(task, table).prs == ()
    explicit = table.model_copy(update={"state_basis": "plan-text"})
    assert _with_table_evidence(task, explicit).prs == ("memory#713",)


def test_gate_blocker_names_its_recorded_cause_and_time() -> None:
    failure = row(
        "STATUS",
        100,
        lane="gate-worker",
        state="gate",
        subject="G0",
        verdict="fail",
        text="first failing check: every task must be merged",
    )
    line = compute_factory_floor(
        request(plans=(wave_plan(),), ledger=(failure,))
    ).lines[0]
    assert line.blocker is not None
    assert "first failing check" in line.blocker.reason
    assert "2026-10-31T10:20:00Z" in line.blocker.reason


def test_cloud_node_snapshot_is_read_only_and_drops_private_fields(
    tmp_path: Path,
) -> None:
    secret = "NEVER-PUBLISH-THIS-SECRET"
    state: dict[str, Any] = {
        "accounts": {
            "account-a": {
                "cap": 4,
                "active": {"run-a": "2026-11-04T19:00:00Z"},
                "credit": {"remaining": 25.5, "read_at": "2026-11-04T19:00:00Z"},
                "token": secret,
            }
        },
        "logins": {
            "account-a": {"transport-a": {"cloud_ok": True, "auth_text": secret}}
        },
        "unconsented": [{"token": secret}],
    }
    path = tmp_path / "cloud-accounts-v2.json"
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    result = HandlerCloudSessionPlacement().handle(
        ModelCloudSessionPlacementRequest(
            action="floor_snapshot", placement_dir=str(tmp_path)
        )
    )
    assert result.error is None
    assert secret not in result.model_dump_json()
    assert path.read_bytes() == before
    assert not (tmp_path / "cloud-accounts.lock").exists()
    assert result.floor_accounts is not None
    (account,) = result.floor_accounts
    assert (account.name, account.login, account.cap, account.credit_remaining) == (
        "account-a",
        "ok",
        4,
        25.5,
    )
    assert account.identity == "account"


def test_missing_snapshot_stays_unread_without_creating_state(tmp_path: Path) -> None:
    result = HandlerCloudSessionPlacement().handle(
        ModelCloudSessionPlacementRequest(
            action="floor_snapshot", placement_dir=str(tmp_path / "missing")
        )
    )
    assert result.error
    assert result.floor_accounts is None
    assert not (tmp_path / "missing").exists()


def test_account_labels_are_drawn_without_machine_name_matching() -> None:
    from omnimarket.models.model_factory_floor_input import ModelFloorCloudAccount

    result = compute_factory_floor(
        request(
            cloud_accounts=(
                ModelFloorCloudAccount(
                    name="account-a", identity="account", login="ok", cap=4
                ),
            )
        )
    )
    (account,) = result.floor.cloud.accounts
    assert account.name == "account-a"
    assert account.ready


def test_cloud_source_calls_the_node_on_the_declared_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import subprocess

    from omnimarket.nodes.node_factory_floor_effect.handlers.handler_cloud_account_source import (
        read_cloud_account_source,
    )
    from omnimarket.nodes.node_factory_floor_effect.source_loader import load_contract
    from tests.nodes.node_factory_floor_effect.test_runtime_factory_floor import (
        CONTRACT,
    )

    source = load_contract(CONTRACT).runtime.cloud_source
    command = ("ssh", "reachable-state-owner", "onex", "node")
    seen: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "status": "success",
                    "result": {
                        "action": "floor_snapshot",
                        "floor_accounts": [
                            {
                                "name": "account-a",
                                "identity": "account",
                                "login": "ok",
                                "cap": 4,
                            }
                        ],
                    },
                }
            ),
            "private diagnostic",
        )

    monkeypatch.setattr(subprocess, "run", run)
    accounts, cause = read_cloud_account_source(tmp_path, source, command)
    assert accounts is not None
    assert accounts[0].name == "account-a"
    assert cause == ""
    argv, kwargs = seen[0]
    assert argv[:4] == list(command)
    assert source.node_name in argv
    assert json.loads(kwargs["input"]) == {"action": "floor_snapshot"}
    assert "/dev/stdin" in argv


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (1, "PRIVATE-NODE-OUTPUT"),
        (0, "PRIVATE-NODE-OUTPUT"),
        (0, '{"status":"failed","error":"PRIVATE-NODE-OUTPUT"}'),
        (0, "[]"),
    ],
)
def test_cloud_source_does_not_publish_subprocess_diagnostics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, returncode: int, stdout: str
) -> None:
    import subprocess

    from omnimarket.nodes.node_factory_floor_effect.handlers.handler_cloud_account_source import (
        read_cloud_account_source,
    )
    from omnimarket.nodes.node_factory_floor_effect.source_loader import load_contract
    from tests.nodes.node_factory_floor_effect.test_runtime_factory_floor import (
        CONTRACT,
    )

    source = load_contract(CONTRACT).runtime.cloud_source
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, returncode, stdout, "PRIVATE-NODE-OUTPUT"
        ),
    )
    accounts, cause = read_cloud_account_source(tmp_path, source)
    assert accounts is None
    assert cause
    assert "PRIVATE-NODE-OUTPUT" not in cause


@pytest.mark.parametrize(
    "state", ["[]", '{"accounts":[]}', '{"accounts":{"a":[]}}', "PRIVATE-INVALID-JSON"]
)
def test_malformed_snapshot_errors_are_safe(tmp_path: Path, state: str) -> None:
    (tmp_path / "cloud-accounts-v2.json").write_text(state)
    result = HandlerCloudSessionPlacement().handle(
        ModelCloudSessionPlacementRequest(
            action="floor_snapshot", placement_dir=str(tmp_path)
        )
    )
    assert result.error
    assert "PRIVATE-INVALID-JSON" not in result.error
    assert result.floor_accounts is None
