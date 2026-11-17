# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam from the marks source to the status view, and the status verb a host off the overlay runs (OMN-19998).

The source host's pool writes its status mirror; a host that is not in the overlay (the launching Mac) reads
that mirror through the same ``MirrorMarksSource`` the rotation tick uses and the status view judges its own
copy against it. The seam is the mirror's key set: the keys the pool writes for each token against the keys
the source adapter reads into ``ModelRotationMark``. A rename on either side makes a mark read as no mark at
all, so the seam test fails when either side changes shape. The status verb is run end to end through
``main`` with the production ports over temporary files: the source mirror is a real file the source host's
own pool wrote, the Mac's pool is a real pool whose local marks are two weeks stale.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.nodes.node_claude_token_pool_effect import (
    runtime_claude_token_pool as cli,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers import (
    handler_claude_token_pool,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.adapter_rotation_ports import (
    MirrorMarksSource,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import (
    HandlerClaudeTokenPool,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool import (
    ModelClaudeTokenPoolRequest,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelRotationMark,
    ModelRotationOverlay,
)
from omnimarket.projection import sqlite_database

pytestmark = pytest.mark.unit

MIRROR = ".cache/omni/claude-token-pool.json"
LABELS = ("acct-a", "onexbot")


def fake_token(label: str) -> str:
    return "sk-ant-" + "oat01-" + (label.replace("-", "x") * 40)[:96]


def overlay(max_age: int = 900) -> ModelRotationOverlay:
    return ModelRotationOverlay.model_validate(
        {
            "hosts": [
                {
                    "name": name,
                    "scheduler": "systemd-user",
                    "venv": "omnimarket/.venv",
                    "token_file": ".config/omni/claude-oauth-token",
                }
                for name in ("h053", "h101")
            ],
            "marks_source": {
                "host": "h053",
                "ssh_target": None,
                "mirror_path": MIRROR,
                "max_age_seconds": max_age,
            },
            "events_file": "token-rotations.jsonl",
            "unit_environment": {},
            "schedule": {"interval_seconds": 300},
        }
    )


def pool_on(
    tmp: Path, host: str, status: Path, now: datetime
) -> HandlerClaudeTokenPool:
    pool = HandlerClaudeTokenPool(
        store=LocalByokCredentialStore(db_path=tmp / host / "store" / "e.sqlite"),
        status_path=status,
        host=host,
        now=lambda: now,
    )
    for label in LABELS:
        pool.handle(
            ModelClaudeTokenPoolRequest(
                operation="add",
                label=label,
                setup_token_output=SecretStr(fake_token(label)),
            )
        )
    return pool


def source_host(home: Path, now: datetime) -> HandlerClaudeTokenPool:
    """The marks-source host's own pool: onexbot was revoked and then cleared by a re-probe; acct-a is limited."""
    pool = pool_on(home, "h053", home / MIRROR, now)
    pool.handle(
        ModelClaudeTokenPoolRequest(
            operation="mark_limited", label="acct-a", seconds=86400
        )
    )
    return pool


def mac_pool(tmp: Path, now: datetime) -> HandlerClaudeTokenPool:
    """The Mac's pool: its marks date from before the source cleared onexbot, and nothing has synced them."""
    pool = pool_on(tmp, "mac", tmp / "mac" / "mirror.json", now - timedelta(days=2))
    pool.handle(ModelClaudeTokenPoolRequest(operation="mark_revoked", label="onexbot"))
    return pool


def test_seam_the_keys_the_pool_writes_are_the_keys_the_source_adapter_reads(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    source_host(tmp_path, now)
    written = json.loads((tmp_path / MIRROR).read_text(encoding="utf-8"))
    row_keys = {key for row in written["tokens"] for key in row}
    assert row_keys == set(ModelRotationMark.model_fields)
    read = MirrorMarksSource(home=tmp_path, mirror_path=MIRROR, ssh_target=None).read()
    assert read.state == "read"
    limited = {m.label: m.until for m in read.marks}
    assert limited["acct-a"] is not None
    assert limited["onexbot"] is None


def test_seam_a_renamed_mirror_key_is_caught_not_read_as_no_mark(
    tmp_path: Path,
) -> None:
    """The failing control: a pool that wrote ``revoked_flag`` for ``revoked`` would read every revoke as cleared."""
    now = datetime.now(UTC)
    pool = source_host(tmp_path, now)
    pool.handle(ModelClaudeTokenPoolRequest(operation="mark_revoked", label="onexbot"))
    path = tmp_path / MIRROR
    body = json.loads(path.read_text(encoding="utf-8"))
    for row in body["tokens"]:
        row["revoked_flag"] = row.pop("revoked_at")
    path.write_text(json.dumps(body), encoding="utf-8")
    read = MirrorMarksSource(home=tmp_path, mirror_path=MIRROR, ssh_target=None).read()
    assert {
        m.label for m in read.marks if m.revoked_at
    } == set()  # the rename hides the revoke
    written_keys = {key for row in body["tokens"] for key in row}
    assert written_keys != set(
        ModelRotationMark.model_fields
    )  # and the seam's key check names it


def test_golden_status_on_a_host_off_the_overlay_reads_the_source_not_its_stale_copy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = datetime.now(UTC)
    source_host(tmp_path, now)
    mac = mac_pool(tmp_path, now)
    before = (tmp_path / "mac" / "store" / "claude_token_pool.json").read_bytes()

    result = cli.run_status("mac", overlay=overlay(), home=tmp_path, pool=mac)

    assert result.basis == "source_marks"
    assert result.marks_source_host == "h053"
    assert result.revoked == ()  # the Mac's own copy still says onexbot is revoked
    assert result.limited == ("acct-a",)
    assert "marks=source:h053" in result.status_line
    assert (
        tmp_path / "mac" / "store" / "claude_token_pool.json"
    ).read_bytes() == before
    capsys.readouterr()


def test_error_status_with_the_source_unreadable_says_unsynced_and_names_no_label(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    mac = mac_pool(tmp_path, now)  # no source mirror file exists under tmp_path
    result = cli.run_status("mac", overlay=overlay(), home=tmp_path, pool=mac)
    assert result.basis == "unsynced_local_copy"
    assert result.revoked is None
    assert result.limited is None
    assert "UNSYNCED" in result.status_line
    assert "revoked=" not in result.status_line
    assert "onexbot" not in result.status_line


def test_error_status_with_a_stale_source_says_unsynced(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    source_host(
        tmp_path, now - timedelta(hours=3)
    )  # the source mirror was written three hours ago
    mac = mac_pool(tmp_path, now)
    result = cli.run_status("mac", overlay=overlay(900), home=tmp_path, pool=mac)
    assert result.basis == "unsynced_local_copy"
    assert "over the 900 s bound" in result.detail


def test_main_status_prints_the_source_line_and_exits_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime.now(UTC)
    source_host(tmp_path, now)
    mac = mac_pool(tmp_path, now)
    real = cli.run_status
    monkeypatch.setattr(
        cli,
        "run_status",
        lambda: real("mac", overlay=overlay(), home=tmp_path, pool=mac),
    )
    assert cli.main(["status"]) == 0
    assert "marks=source:h053" in capsys.readouterr().out


def test_error_main_status_prints_unsynced_exits_three_and_never_names_a_revoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime.now(UTC)
    mac = mac_pool(tmp_path, now)
    real = cli.run_status
    monkeypatch.setattr(
        cli,
        "run_status",
        lambda: real("mac", overlay=overlay(), home=tmp_path, pool=mac),
    )
    assert cli.main(["status"]) == cli.EXIT_EMPTY
    out = capsys.readouterr().out
    assert "UNSYNCED" in out
    assert "revoked=" not in out
    assert "onexbot" not in out
    monkeypatch.setattr(
        cli,
        "run_status",
        lambda: real("mac", overlay=overlay(), home=tmp_path, pool=mac),
    )
    assert cli.main(["status", "--json"]) == cli.EXIT_EMPTY
    body = json.loads(capsys.readouterr().out)
    assert body["basis"] == "unsynced_local_copy"
    assert body["revoked"] is None
    assert body["limited"] is None


def test_failing_control_the_old_status_names_the_revoke_the_source_cleared(
    tmp_path: Path,
) -> None:
    """The old behaviour, the pool's own status on any host: the Mac still prints the stale revoke."""
    now = datetime.now(UTC)
    source_host(tmp_path, now)
    mac = mac_pool(tmp_path, now)
    old = mac.handle(ModelClaudeTokenPoolRequest(operation="status", dry_run=True))
    assert old.revoked == ("onexbot",)
    assert "revoked=onexbot" in old.status_line


def test_status_entries_prints_the_pool_result_not_the_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Through ``main`` on the pool it builds itself: --entries prints the pool's own entries, plain status still prints the view.

    A handler argument cannot test this (it bypasses the flag: the plain read answers the same pool),
    so the pool is isolated where its files are found on a host: the two locations the modules took
    from ``$HOME`` at import are pointed under ``tmp_path``, and the entries are seeded through ``add``.
    """
    # No pool overlay under these roots: the plain status answers unsynced (the view), never a marks source.
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    monkeypatch.setenv("ONEX_WORKSPACE_CONFIG_ROOT", str(tmp_path))
    monkeypatch.setattr(
        sqlite_database,
        "_DEFAULT_EVIDENCE_DB_PATH",
        tmp_path / ".omninode" / "delegation" / "delegation.sqlite",
    )
    monkeypatch.setattr(
        handler_claude_token_pool,
        "STATUS_MIRROR",
        tmp_path / ".cache" / "omni" / "claude-token-pool.json",
    )
    for label in LABELS:
        monkeypatch.setattr("sys.stdin", io.StringIO(fake_token(label)))
        assert cli.main(["add", "--label", label]) == 0
    capsys.readouterr()
    assert cli.main(["status", "--json", "--entries"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert isinstance(body["tokens"], list)
    assert [t["label"] for t in body["tokens"]] == list(LABELS)
    assert all(t["fingerprint"] for t in body["tokens"])
    capsys.readouterr()
    assert cli.main(["status", "--json"]) == cli.EXIT_EMPTY
    view = json.loads(capsys.readouterr().out)
    assert view["basis"] == "unsynced_local_copy"
    assert isinstance(view["tokens"], int)
    assert view["tokens"] == len(LABELS)
