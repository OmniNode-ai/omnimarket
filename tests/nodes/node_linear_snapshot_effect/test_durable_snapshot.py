# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A missing daily snapshot is a failed source, and the tick maintains the writer."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from omnimarket.inference import secret_store_resolver
from omnimarket.nodes.node_idle_slot_fill_effect.handlers import (
    handler_idle_slot_fill as fill,
)
from omnimarket.nodes.node_idle_slot_fill_effect.models.model_idle_slot_fill import (
    ModelBacklogSources,
    ModelLabFillRequest,
)
from omnimarket.nodes.node_idle_slot_fill_effect.models.model_ruling_recovery import (
    ModelRulingRecoveryResult,
)
from omnimarket.nodes.node_linear_snapshot_effect.handlers import (
    handler_linear_snapshot as writer,
)
from omnimarket.nodes.node_linear_snapshot_effect.models import (
    ModelLinearSnapshotRequest,
)

NOW = "2026-11-04T19:03:00Z"


def sources(path: Path) -> ModelBacklogSources:
    return ModelBacklogSources(
        linear_snapshot=path,
        ledger_tickets=lambda: (set(), {}),
        ticket_rule=lambda _: {"items": [], "drops": []},
    )


def snapshot(path: Path, at: str, **extra: Any) -> None:
    path.write_text(json.dumps({"operator": "operator-id", "issues": [], **extra}))
    stamp = datetime.fromisoformat(at).timestamp()
    os.utime(path, (stamp, stamp))


def test_recorded_november_first_snapshot_is_missing_on_november_fourth(
    tmp_path: Path,
) -> None:
    snapshot(tmp_path / "linear-2026-11-01.json", "2026-11-01T19:03:00Z")
    items, line = fill.read_generated(
        sources(tmp_path / "linear-2026-11-04.json"), {}, {}, NOW
    )
    assert items == []
    assert "MISSING linear_snapshot" in line
    assert "age=259200s" in line
    assert "tickets=MISSING" in line
    assert "tickets=0" not in line


@pytest.mark.parametrize("age", [1801, 3600])
def test_same_day_stale_snapshot_is_missing(tmp_path: Path, age: int) -> None:
    path = tmp_path / "linear-2026-11-04.json"
    stamp = datetime.fromisoformat(NOW).timestamp() - age
    snapshot(path, datetime.fromtimestamp(stamp, UTC).isoformat())
    _, line = fill.read_generated(sources(path), {}, {}, NOW)
    assert "MISSING linear_snapshot" in line
    assert f"age={age}s" in line
    assert "tickets=MISSING" in line


def test_fresh_empty_snapshot_is_observed_empty(tmp_path: Path) -> None:
    path = tmp_path / "linear-2026-11-04.json"
    snapshot(path, "2026-11-04T19:02:00Z")
    _, line = fill.read_generated(sources(path), {}, {}, NOW)
    assert "tickets=0" in line
    assert "MISSING" not in line


@pytest.mark.parametrize("payload", ["{", "{}", "[]", '{"operator":"me","issues":[]}'])
def test_invalid_snapshot_is_missing_with_age(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "linear-2026-11-04.json"
    path.write_text(payload)
    stamp = datetime.fromisoformat("2026-11-04T19:02:00Z").timestamp()
    os.utime(path, (stamp, stamp))
    _, line = fill.read_generated(sources(path), {}, {}, NOW)
    assert "MISSING linear_snapshot" in line
    assert "age=60s" in line


def test_no_snapshot_history_has_unknown_age(tmp_path: Path) -> None:
    _, line = fill.read_generated(
        sources(tmp_path / "linear-2026-11-04.json"), {}, {}, NOW
    )
    assert "MISSING linear_snapshot age=unknown" in line


def test_generated_time_survives_copying_a_stale_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "linear-2026-11-04.json"
    snapshot(path, "2026-11-04T19:02:00Z", generated_at="2026-11-04T18:03:00Z")
    _, line = fill.read_generated(sources(path), {}, {}, NOW)
    assert "tickets=MISSING" in line
    assert "age=3600s" in line


def test_writer_resolves_store_once_for_all_pages_without_shell_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    calls: list[str] = []
    pages: list[dict[str, Any]] = []

    def resolve(reference: str) -> SecretStr:
        calls.append(reference)
        return SecretStr("store-only-sentinel")

    def post(
        url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: float
    ) -> httpx.Response:
        assert headers["Authorization"] == "store-only-sentinel"
        pages.append(json["variables"])
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "data": {
                    "viewer": {"id": "operator-id"},
                    "issues": {
                        "nodes": [],
                        "pageInfo": {
                            "hasNextPage": len(pages) == 1,
                            "endCursor": "second",
                        },
                    },
                }
            },
        )

    monkeypatch.setattr(secret_store_resolver, "resolve_api_key_loop_safe", resolve)
    monkeypatch.setattr(httpx, "post", post)
    result = writer.HandlerLinearSnapshot().handle(
        ModelLinearSnapshotRequest(out_dir=str(tmp_path))
    )
    assert result.status == "written"
    assert calls == ["LINEAR_API_KEY"]
    assert len(pages) == 2
    doc = json.loads(Path(result.path).read_text())
    assert datetime.fromisoformat(doc["generated_at"]).tzinfo is not None
    assert "store-only-sentinel" not in result.model_dump_json()


@pytest.mark.parametrize("error", [RuntimeError, writer.SnapshotReadError])
def test_writer_does_not_return_transport_error_text(
    tmp_path: Path, error: type[Exception]
) -> None:
    def fail(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        raise error("sensitive-transport-sentinel")

    result = writer.HandlerLinearSnapshot(fail).handle(
        ModelLinearSnapshotRequest(out_dir=str(tmp_path))
    )
    assert result.status == "failed"
    assert "sensitive-transport-sentinel" not in result.model_dump_json()


def test_refresh_writes_missing_snapshot_then_reuses_fresh_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def post(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        calls.append(variables)
        return {
            "viewer": {"id": "operator-id"},
            "issues": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        }

    monkeypatch.setattr(
        fill,
        "HandlerLinearSnapshot",
        lambda: writer.HandlerLinearSnapshot(post),
        raising=False,
    )
    path = tmp_path / "linear-2026-11-04.json"
    src = sources(path)
    fill.refresh_linear_snapshot(src, NOW)
    assert path.exists()
    assert len(calls) == 1
    now = datetime.now(UTC).isoformat()
    fill.refresh_linear_snapshot(src, now)
    assert len(calls) == 1
    _, line = fill.read_generated(src, {}, {}, now)
    assert "tickets=0" in line
    assert "MISSING" not in line


@pytest.mark.parametrize("read_only", [False, True])
def test_tick_refreshes_default_snapshot_without_headroom_but_read_only_never_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_only: bool
) -> None:
    path = tmp_path / "linear-2026-11-04.json"
    calls = []
    overlay = tmp_path / "overlays" / "idle_slot_fill"
    overlay.mkdir(parents=True)
    (overlay / "overlay.yaml").write_text("repo_required_kinds: []\n")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(overlay.parent))

    def post(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        calls.append(variables)
        return {
            "viewer": {"id": "operator-id"},
            "issues": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        }

    monkeypatch.setattr(
        fill, "HandlerLinearSnapshot", lambda: writer.HandlerLinearSnapshot(post)
    )
    monkeypatch.setattr(fill, "default_sources", lambda _now, _env: sources(path))
    monkeypatch.setattr(
        fill.HandlerRulingRecovery,
        "handle",
        lambda _self, **_kw: ModelRulingRecoveryResult([], []),
    )
    result = fill.HandlerLabFill().handle(
        ModelLabFillRequest(
            now=NOW,
            request={"lab_headroom": {"hosts": []}},
            lines=[],
            launch=None,
            state_dir=tmp_path / "state",
            backlog=None,
            runner=tmp_path / "unused-runner",
            lanes_dir=tmp_path / "receipts",
            read_only=read_only,
            out_of_scope={},
        )
    )
    assert path.exists() is (not read_only)
    assert len(calls) == (0 if read_only else 1)
    assert any(loop == "linear-snapshot" for _, loop, _, _ in result.found) is (
        not read_only
    )


def test_fsync_failure_keeps_previous_snapshot_and_removes_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "linear-2026-11-04.json"
    target.write_text("previous-snapshot")

    def post(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return {
            "viewer": {"id": "operator-id"},
            "issues": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        }

    def fail_sync(fd: int) -> None:
        raise OSError("sensitive-disk-error-sentinel")

    monkeypatch.setattr(writer.os, "fsync", fail_sync)
    result = writer.HandlerLinearSnapshot(post).handle(
        ModelLinearSnapshotRequest(out_dir=str(tmp_path), date="2026-11-04")
    )
    assert result.status == "failed"
    assert result.reason == "OSError"
    assert target.read_text() == "previous-snapshot"
    assert list(tmp_path.iterdir()) == [target]


def test_refresh_retries_after_fifteen_minutes_before_snapshot_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "linear-2026-11-04.json"
    snapshot(path, "2026-11-04T18:47:59Z")
    calls = []

    class FailedWriter:
        def handle(
            self, request: ModelLinearSnapshotRequest
        ) -> writer.ModelLinearSnapshotResult:
            calls.append(request)
            return writer.ModelLinearSnapshotResult(
                status="failed",
                path=str(path),
                issue_count=0,
                with_description=0,
                reason="SnapshotReadError",
            )

    monkeypatch.setattr(fill, "HandlerLinearSnapshot", FailedWriter)
    note = fill.refresh_linear_snapshot(sources(path), NOW)
    assert len(calls) == 1
    assert "failed" in note
    _, line = fill.read_generated(sources(path), {}, {}, NOW)
    assert "tickets=0" in line
    assert "MISSING" not in line
