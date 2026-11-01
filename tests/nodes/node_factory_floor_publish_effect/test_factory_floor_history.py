# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor publish node keeps every published record: an append-only, retention-bound history."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_factory_floor_publish_effect.handlers import (
    handler_factory_floor_publish as handler_module,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    HandlerFactoryFloorPublish,
    load_runtime,
    prunable_snapshots,
    snapshot_name,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishOverlay,
    ModelFactoryFloorPublishRequest,
    ModelFactoryFloorPublishResult,
    ModelHistoryRetention,
    ModelPublishHistory,
)

pytestmark = pytest.mark.unit

FIRST_AT = datetime(2026, 11, 1, 3, 0, 0, tzinfo=UTC)
HISTORY = ModelPublishHistory(
    subdirectory="history",
    timestamp_format="%Y%m%dT%H%M%SZ",
    retention=ModelHistoryRetention(days=30, max_count=3),
)


def _record(marker: str) -> dict[str, object]:
    return {
        "generated_at": "2026-11-01T03:00:00+00:00",
        "lines": [],
        "marker": marker,
    }


class LocalSsh:
    """Stands in for ssh: the remote command runs in a local shell, so writes, listing and removal really execute."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def __call__(
        self, argv: Sequence[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        if argv[0] != "ssh":
            return subprocess.CompletedProcess(list(argv), 0, b"", b"")
        self.commands.append(argv[-1])
        return subprocess.run(
            ["sh", "-c", argv[-1]], input=stdin, capture_output=True, check=False
        )


def _publish(
    tmp_path: Path, record: dict[str, object], now: datetime, ssh: LocalSsh
) -> ModelFactoryFloorPublishResult:
    home = tmp_path / "home"
    target = home / load_runtime().source.relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record), encoding="utf-8")
    site = tmp_path / "site"
    request = ModelFactoryFloorPublishRequest(
        home=str(home),
        skip_refresh=True,
        now=now,
        overlay=ModelFactoryFloorPublishOverlay(
            ssh_target="lab-web",
            remote_dir=str(site),
            serve_url="http://example.invalid:8088/",
            bus_lane="lane-x",
            history_root=str(site),
        ),
    )
    return HandlerFactoryFloorPublish(ssh).handle(request, environ={})


def test_history_snapshot_is_written_beside_the_live_record_with_a_utc_timestamped_name(
    tmp_path: Path,
) -> None:
    contract = load_runtime().history
    moment = datetime(2026, 11, 1, 5, 0, 7, 250, tzinfo=UTC)
    ssh = LocalSsh()
    result = _publish(tmp_path, _record("a"), moment, ssh)
    expected = snapshot_name(contract, "factory-floor.json", moment)
    assert expected == "history/factory-floor-20261101T050007000250Z.json"
    site = tmp_path / "site"
    assert (site / expected).read_bytes() == (site / "factory-floor.json").read_bytes()
    assert result.history_snapshot == expected
    assert [p.name for p in (site / "history").iterdir()] == [
        "factory-floor-20261101T050007000250Z.json"
    ]


def test_history_name_uses_utc_whatever_the_zone_of_the_clock() -> None:
    local = datetime(2026, 11, 1, 5, 0, 7, tzinfo=UTC).astimezone(
        timezone(timedelta(hours=2))
    )
    assert snapshot_name(HISTORY, "factory-floor.json", local) == (
        "history/factory-floor-20261101T050007Z.json"
    )


def test_history_second_publish_keeps_the_first_snapshot_byte_identical(
    tmp_path: Path,
) -> None:
    """Falsifier: fails on a version that overwrites or deletes the first snapshot."""
    ssh = LocalSsh()
    _publish(tmp_path, _record("first"), FIRST_AT, ssh)
    site = tmp_path / "site"
    first = next((site / "history").iterdir())
    first_bytes = first.read_bytes()
    assert json.loads(first_bytes)["marker"] == "first"

    _publish(tmp_path, _record("second"), FIRST_AT + timedelta(minutes=5), ssh)

    assert first.exists()
    assert first.read_bytes() == first_bytes
    assert len(list((site / "history").iterdir())) == 2
    assert json.loads((site / "factory-floor.json").read_text())["marker"] == "second"


def test_history_a_snapshot_name_already_taken_is_refused_and_kept(
    tmp_path: Path,
) -> None:
    ssh = LocalSsh()
    _publish(tmp_path, _record("first"), FIRST_AT, ssh)
    first = next((tmp_path / "site" / "history").iterdir())
    kept = first.read_bytes()
    with pytest.raises(Exception, match="history/factory-floor"):
        _publish(tmp_path, _record("second"), FIRST_AT, ssh)
    assert first.read_bytes() == kept
    live = json.loads((tmp_path / "site" / "factory-floor.json").read_text())
    assert live["marker"] == "first"


def _names(moments: Sequence[datetime]) -> list[str]:
    return [
        snapshot_name(HISTORY, "factory-floor.json", m).split("/", 1)[1]
        for m in moments
    ]


def test_retention_prunes_only_past_window_snapshots() -> None:
    now = FIRST_AT
    old = [now - timedelta(days=40 + i) for i in range(3)]
    young = [now - timedelta(days=1 + i) for i in range(3)]
    names = [*_names(old + young), "notes.txt", "factory-floor-bad.json"]
    pruned = prunable_snapshots(HISTORY, "factory-floor.json", names, now)
    assert set(pruned) == set(_names(old))
    assert not set(pruned) & set(_names(young))
    assert "notes.txt" not in pruned


def test_retention_never_prunes_a_snapshot_inside_the_window_even_over_the_count() -> (
    None
):
    now = FIRST_AT
    young = [now - timedelta(days=29, hours=h) for h in range(10)]
    assert prunable_snapshots(HISTORY, "factory-floor.json", _names(young), now) == ()


def test_retention_keeps_past_window_snapshots_while_the_count_allows() -> None:
    now = FIRST_AT
    old = [now - timedelta(days=40 + i) for i in range(2)]
    assert prunable_snapshots(HISTORY, "factory-floor.json", _names(old), now) == ()


def test_retention_drops_the_oldest_past_window_first_down_to_the_count() -> None:
    now = FIRST_AT
    old = [now - timedelta(days=40 + i) for i in range(4)]
    pruned = prunable_snapshots(HISTORY, "factory-floor.json", _names(old), now)
    assert pruned == (_names(old)[3],)


def test_retention_publish_prunes_past_window_files_and_never_the_live_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = load_runtime()
    monkeypatch.setattr(
        handler_module,
        "load_runtime",
        lambda: real.model_copy(update={"history": HISTORY}),
    )
    site = tmp_path / "site"
    (site / "history").mkdir(parents=True)
    ancient = FIRST_AT - timedelta(days=90)
    stale = [ancient - timedelta(days=i) for i in range(5)]
    for name in _names(stale):
        (site / "history" / name).write_text("{}", encoding="utf-8")
    (site / "history" / "keep-me.txt").write_text("x", encoding="utf-8")
    ssh = LocalSsh()
    result = _publish(tmp_path, _record("now"), FIRST_AT, ssh)
    left = sorted(p.name for p in (site / "history").iterdir())
    pruned = result.history_pruned
    assert len(pruned) == 3
    assert "keep-me.txt" in left
    assert len(left) == 3 + 1
    assert any(
        "now" in (site / "history" / n).read_text() for n in left if n != "keep-me.txt"
    )
    assert json.loads((site / "factory-floor.json").read_text())["marker"] == "now"


def test_overlay_or_missing_key_history_root_missing_is_refused_with_the_overlay_error(
    tmp_path: Path,
) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        "ssh_target: lab-web\nremote_dir: /srv/site\nserve_url: u\nbus_lane: lane-x\n",
        encoding="utf-8",
    )
    ssh = LocalSsh()
    request = ModelFactoryFloorPublishRequest(home=str(tmp_path), skip_refresh=True)
    with pytest.raises(NodeOverlayError, match="history_root"):
        HandlerFactoryFloorPublish(ssh).handle(
            request, environ={"ONEX_FACTORY_FLOOR_PUBLISH_OVERLAY": str(overlay)}
        )
    assert ssh.commands == []


def test_history_contract_declares_names_and_retention_but_no_host_or_path() -> None:
    history = load_runtime().history
    assert history.subdirectory == "history"
    assert history.retention.days > 0
    assert history.retention.max_count > 0
    assert not history.subdirectory.startswith("/")
