# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""detail_expiry: ledger detail files expire after the declared days; one a ledger row cites stays.

Driven through the contract's own ``ledger-detail`` entry (its keep_days and reference_paths), with
the registry root and home pointed at a temporary tree. The row that cites a detail may sit in the
live ledger or in a rolled archive of it, because the ledger rolls and a roll is still a ledger.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_retention_cleanup.handlers import (
    handler_file_retention as mod,
)
from omnimarket.nodes.node_retention_cleanup.handlers.handler_file_retention import (
    FileRetentionRequest,
    NodeFileRetention,
    load_file_policy,
)

_ROOT = Path(__file__).resolve().parents[2]
_CONTRACT = _ROOT / "src/omnimarket/nodes/node_retention_cleanup/contract.yaml"
_TIMER = _ROOT / "scripts/retention/onex-retention-cleanup.timer.template"
_SERVICE = _ROOT / "scripts/retention/onex-retention-cleanup.service.template"
NOW = dt.datetime(2026, 11, 20, 12, 0, tzinfo=dt.UTC)


def _old(path: Path, text: str, days: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = (NOW - dt.timedelta(days=days)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def _entry() -> mod.FileRetentionEntry:
    return next(
        e for e in load_file_policy(_CONTRACT).entries if e.name == "ledger-detail"
    )


def test_detail_expiry_is_declared_scheduled_at_seven_days() -> None:
    entry = _entry()
    assert entry.keep_days == 7
    assert entry.action == "archive"
    assert "node_retention_cleanup --files" in _SERVICE.read_text(encoding="utf-8")
    assert "OnCalendar=" in _TIMER.read_text(encoding="utf-8")
    raw = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert raw["retention_policy"]["file_retention"]["archive_root_env"]


def test_detail_expiry_removes_old_uncited_and_keeps_old_cited_including_in_a_rolled_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    omni = tmp_path / "omni"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OMNI_HOME", str(omni))
    detail = omni / ".claude_scratch/ledger-detail"
    uncited = _old(detail / "query.AAAAAA", "uncited output\n", days=9)
    cited_live = _old(detail / "query.BBBBBB", "cited live\n", days=9)
    cited_rolled = _old(detail / "query.CCCCCC", "cited in a roll\n", days=9)
    young = _old(detail / "query.DDDDDD", "young\n", days=1)
    # The reference files come from the entry's own declared reference_paths: the live ledger is
    # the one path with no glob, a roll is the declared archive glob with its star filled in.
    declared = [Path(os.path.expandvars(p)) for p in _entry().reference_paths]
    live_ref = next(p for p in declared if "*" not in p.name and "tracking" in p.parts)
    roll_glob = next(p for p in declared if "archive" in p.parts and "*" in p.name)
    roll_ref = roll_glob.with_name(roll_glob.name.replace("*", "2026-11-14-split"))
    live_ref.parent.mkdir(parents=True)
    live_ref.write_text(
        f"2026-11-19T00:00:00Z | STATUS | detail={cited_live}\n", encoding="utf-8"
    )
    roll_ref.parent.mkdir(parents=True)
    roll_ref.write_text(
        f"2026-11-14T00:00:00Z | STATUS | detail={cited_rolled}\n", encoding="utf-8"
    )
    archive = tmp_path / "archive-root"
    archive.mkdir()
    monkeypatch.setattr(mod, "_has_marker", lambda *_: True)
    result = NodeFileRetention().handle(
        FileRetentionRequest(
            entries=[_entry()],
            now=NOW,
            dry_run=False,
            host="h-test",
            archive_root=str(archive),
            synced_marker="test.synced",
        )
    )
    assert [i.status for i in result.items] == ["ok"], result
    assert not uncited.exists()
    assert cited_live.exists()
    assert cited_rolled.exists()
    assert young.exists()
