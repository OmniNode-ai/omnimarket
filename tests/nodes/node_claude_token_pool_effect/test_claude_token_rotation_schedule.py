# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The rotation tick owns its timer, reads its marks through real ports, and runs from the command line (OMN-23000)."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.nodes.node_claude_token_pool_effect import (
    runtime_claude_token_pool as cli,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.adapter_rotation_ports import (
    MirrorMarksSource,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import (
    HandlerClaudeTokenPool,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.schedule import (
    CONTRACT_PATH,
    RotationScheduler,
    load_schedule,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool import (
    ModelClaudeTokenPoolRequest,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelRotationOverlay,
)

pytestmark = pytest.mark.unit

NODE = CONTRACT_PATH.parent


def fake_token(label: str) -> str:
    return "sk-ant-" + "oat01-" + (label.replace("-", "x") * 40)[:96]


def example_overlay() -> ModelRotationOverlay:
    return ModelRotationOverlay.model_validate(
        yaml.safe_load((NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    )


class Runs:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command):
        self.commands.append(list(command))
        return subprocess.CompletedProcess(list(command), 0, "", "")


def scheduler(tmp: Path, runs: Runs) -> RotationScheduler:
    return RotationScheduler(
        schedule=load_schedule(),
        overlay=example_overlay(),
        home=tmp / "home",
        run=runs,
        uid=501,
    )


def test_contract_declares_the_schedule_and_the_example_overlay_loads() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert contract["schedule"]["unit_name"] == "onex-claude-token-rotation"
    assert example_overlay().rank("mac") == 2


def test_dry_run_renders_a_systemd_timer_and_a_launchd_agent_that_run_the_tick(
    tmp_path: Path,
) -> None:
    runs = Runs()
    sched = scheduler(tmp_path, runs)
    linux = sched.install(
        "host-b",
        omni_root=tmp_path / "omni",
        overlay_root=tmp_path / "ov",
        dry_run=True,
    )
    assert linux.exit_code == 0
    assert "tick --host host-b" in linux.stdout
    assert "OnUnitActiveSec=300s" in linux.stdout
    mac = sched.install(
        "mac", omni_root=tmp_path / "omni", overlay_root=tmp_path / "ov", dry_run=True
    )
    assert mac.exit_code == 0
    assert "<key>StartInterval</key>" in mac.stdout
    assert "<integer>300</integer>" in mac.stdout
    assert "<string>tick</string>" in mac.stdout
    assert "<string>mac</string>" in mac.stdout
    assert runs.commands == []  # a dry run starts and writes nothing


def test_an_undeclared_host_is_refused_and_uninstall_rolls_the_units_back(
    tmp_path: Path,
) -> None:
    runs = Runs()
    sched = scheduler(tmp_path, runs)
    refused = sched.install(
        "h999", omni_root=tmp_path / "omni", overlay_root=tmp_path / "ov"
    )
    assert refused.exit_code == 1
    assert "not in the" in refused.stderr
    sched.unit_dir.mkdir(parents=True)
    for name in (sched.service_name, sched.timer_name):
        (sched.unit_dir / name).write_text("x", encoding="utf-8")
    done = sched.uninstall("host-b")
    assert done.exit_code == 0
    assert not (sched.unit_dir / sched.timer_name).exists()
    assert [
        "systemctl",
        "--user",
        "disable",
        "--now",
        sched.timer_name,
    ] in runs.commands


def mirror(path: Path, at: datetime, tokens: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"host": "h053", "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "tokens": tokens}
        ),
        encoding="utf-8",
    )


def test_mirror_source_reads_in_place_over_ssh_and_names_a_bad_mirror(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 11, 7, 14, 0, tzinfo=UTC)
    rows = [{"label": "caia", "until": "2026-11-09T22:00:00Z", "revoked": False}]
    mirror(tmp_path / "m.json", now, rows)
    local = MirrorMarksSource(
        home=tmp_path, mirror_path="m.json", ssh_target=None
    ).read()
    assert local.state == "read"
    assert local.marks[0].label == "caia"

    seen: list[list[str]] = []

    def over_ssh(command):
        seen.append(list(command))
        return subprocess.CompletedProcess(
            list(command), 0, (tmp_path / "m.json").read_text(), ""
        )

    remote = MirrorMarksSource(
        home=tmp_path,
        mirror_path="m.json",
        ssh_target="h053.example.invalid",
        run=over_ssh,
    ).read()
    assert remote.state == "read"
    assert seen[0][0] == "ssh"
    assert "BatchMode=yes" in seen[0]

    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    bad = MirrorMarksSource(
        home=tmp_path, mirror_path="bad.json", ssh_target=None
    ).read()
    assert bad.state == "unreadable"
    gone = MirrorMarksSource(
        home=tmp_path, mirror_path="absent.json", ssh_target=None
    ).read()
    assert gone.state == "unreadable"

    def down(command):
        return subprocess.CompletedProcess(list(command), 255, "", "")

    assert (
        MirrorMarksSource(home=tmp_path, mirror_path="m.json", ssh_target="x", run=down)
        .read()
        .detail
        == "ssh exit 255"
    )


def test_run_tick_from_the_command_swaps_a_limited_login_through_the_production_ports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    pool = HandlerClaudeTokenPool(
        store=LocalByokCredentialStore(db_path=tmp_path / "store" / "e.sqlite"),
        status_path=tmp_path / "cache" / "claude-token-pool.json",
        host="host-b",
    )
    for label in ("acct-a", "caia"):
        pool.handle(
            ModelClaudeTokenPoolRequest(
                operation="add",
                label=label,
                setup_token_output=SecretStr(fake_token(label)),
            )
        )
    login = home / ".config" / "omni" / "claude-oauth-token"
    login.parent.mkdir(parents=True)
    login.write_text(fake_token("caia") + "\n", encoding="utf-8")
    # the source host's mirror says caia is limited; this host never saw the limit
    until = (datetime.now(UTC) + timedelta(hours=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    mirror(
        home / ".cache" / "omni" / "claude-token-pool.json",
        datetime.now(UTC),
        [{"label": "caia", "until": until, "revoked": False}],
    )
    overlay = example_overlay().model_copy(
        update={
            "marks_source": example_overlay().marks_source.model_copy(
                update={"ssh_target": None}
            )
        }
    )
    monkeypatch.setattr(cli, "load_overlay", lambda: overlay)
    monkeypatch.setenv("OMNI_HOME", str(tmp_path / "omni"))
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(cli, "HandlerClaudeTokenPool", lambda **_kw: pool)
    code = cli.main(["tick", "--host", "host-b"])
    out = capsys.readouterr()
    assert code == 0
    assert "outcome=swapped" in out.out
    assert "from=caia" in out.out
    assert "to=acct-a" in out.out
    assert login.read_text().strip() == fake_token("acct-a")
    events = (
        tmp_path
        / "omni"
        / ".onex_state"
        / "claude-token-rotation"
        / "token-rotations.jsonl"
    ).read_text()
    assert '"kind":"rotated"' in events
    for label in ("acct-a", "caia"):
        assert fake_token(label) not in out.out + out.err + events


def test_tick_for_an_undeclared_host_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_overlay", example_overlay)
    code = cli.main(["tick", "--host", "h999"])
    assert code == 3
    assert "INDETERMINATE" in capsys.readouterr().err


def test_example_overlay_declares_the_spread_block() -> None:
    spread = example_overlay().spread
    assert spread is not None
    assert spread.enabled is True
    assert spread.in_use_longer_than_seconds >= 60


def test_overlay_refuses_an_unknown_spread_key() -> None:
    raw = yaml.safe_load((NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    raw["spread"]["unknown_key"] = 1
    with pytest.raises(ValueError, match="unknown_key"):
        ModelRotationOverlay.model_validate(raw)
