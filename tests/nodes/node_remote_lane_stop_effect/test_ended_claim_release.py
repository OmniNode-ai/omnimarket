# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ended-engine cleanup through the registry and the real stop adapter."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from omnimarket.models.lane_closeout.model_lane_closeout_run_request import (
    ModelLaneCloseoutRunRequest,
)
from omnimarket.models.pr_claim import (
    ModelPrClaimRegistryRequest,
    ModelPrClaimRegistryResult,
)
from omnimarket.nodes.node_lane_closeout_orchestrator.handlers.handler_lane_closeout_run import (
    HandlerLaneCloseoutRun,
)
from omnimarket.nodes.node_pr_claim_registry_effect.handlers.handler_pr_claim_registry import (
    HandlerPrClaimRegistry,
    filesystem_key,
)
from omnimarket.nodes.node_remote_lane_stop_effect.handlers.handler_remote_lane_stop import (
    create_handler,
)
from omnimarket.nodes.node_remote_lane_stop_effect.models.model_remote_lane_stop import (
    ModelRemoteLaneStopRequest,
    ModelStopHost,
)
from tests.nodes.lane_closeout_support import run_over_profile
from tests.nodes.node_remote_lane_stop_effect.conftest import (
    LANE,
    RUN_ID,
    write_receipt,
)

pytestmark = pytest.mark.unit
NOW = "2026-11-04T20:00:00Z"
SESSION = "engine-session-1"


def _claim(directory: Path, number: int = 1, **fields: object) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    key = f"example/repo#{number}"
    path = directory / f"{filesystem_key(key)}.json"
    path.write_text(
        json.dumps(
            {
                "pr_key": key,
                "claimed_by_run": RUN_ID,
                "lane_id": LANE,
                "claimed_by_session": SESSION,
                "claimed_at": NOW,
                "last_heartbeat_at": NOW,
                **fields,
            }
        )
    )
    return path


def _registry(
    directory: Path, operation: str = "release_ended_own_claims", **fields: object
) -> ModelPrClaimRegistryResult:
    return HandlerPrClaimRegistry().handle(
        ModelPrClaimRegistryRequest.model_validate(
            {
                "operation": operation,
                "claims_dir": str(directory),
                "now": NOW,
                "run_id": RUN_ID,
                "lane_id": LANE,
                "session_id": SESSION,
                **fields,
            }
        )
    )


def test_explicit_release_preserves_another_session(tmp_path: Path) -> None:
    path = _claim(tmp_path, claimed_by_session="replacement-session")
    result = _registry(tmp_path, "release", pr_key="example/repo#1")
    assert not result.succeeded
    assert path.exists()


def test_public_registry_closeout_preserves_peer_claims(tmp_path: Path) -> None:
    from omnimarket.nodes.node_pr_claim_registry_effect import HandlerPrClaimCloseout

    own = _claim(tmp_path)
    peer = _claim(tmp_path, 2, claimed_by_session="live-session")
    before = peer.read_bytes()
    result = HandlerPrClaimCloseout().handle(
        ModelPrClaimRegistryRequest.model_validate(
            {
                "operation": "release_ended_own_claims",
                "claims_dir": str(tmp_path),
                "now": NOW,
                "run_id": RUN_ID,
                "lane_id": LANE,
                "session_id": SESSION,
            }
        )
    )
    assert result.pr_keys == ("example/repo#1",)
    assert not own.exists()
    assert peer.read_bytes() == before


def test_cleanup_releases_only_exact_ended_identity(tmp_path: Path) -> None:
    own = _claim(tmp_path)
    stale_own = _claim(
        tmp_path,
        2,
        claimed_at="2026-01-01T00:00:00Z",
        last_heartbeat_at="2026-01-01T00:00:00Z",
    )
    peers = [
        _claim(tmp_path, 3, lane_id="live-peer"),
        _claim(tmp_path, 4, claimed_by_session="live-session"),
        _claim(tmp_path, 5, claimed_by_run="live-run"),
        _claim(tmp_path, 6, lane_id=None),
        _claim(tmp_path, 7, claimed_by_session=None),
    ]
    before = {p: p.read_bytes() for p in peers}
    result = _registry(tmp_path)
    assert result.pr_keys == ("example/repo#1", "example/repo#2")
    assert not own.exists()
    assert not stale_own.exists()
    assert {p: p.read_bytes() for p in peers} == before
    assert result.claims == ()
    assert _registry(tmp_path).pr_keys == ()


@pytest.mark.parametrize("field", ["lane_id", "run_id", "session_id"])
def test_missing_identity_refuses_cleanup(tmp_path: Path, field: str) -> None:
    path = _claim(tmp_path)
    result = _registry(tmp_path, **{field: ""})
    assert not result.succeeded
    assert result.pr_keys == ()
    assert path.exists()


def test_stopped_engine_closeout_uses_real_adapters(
    tmp_path: Path, receipts: Path, ledger: Path, ledger_tool: list[str]
) -> None:
    claims = tmp_path / "claims"
    own = _claim(claims, claimed_by_run="mutation-run")
    peer = _claim(claims, 2, lane_id="live-peer")
    peer_bytes = peer.read_bytes()
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True
    )
    try:
        write_receipt(
            receipts,
            host="local",
            pid=child.pid,
            session_id=SESSION,
            claim_run_id="mutation-run",
        )
        request = ModelRemoteLaneStopRequest(
            lane=LANE,
            reason="ended-engine claim cleanup",
            stopping_lane="orchestrator-1",
            receipt_dir=str(receipts),
            ledger_path=str(ledger),
            ledger_command=ledger_tool,
            actor="codex:test",
            model="codex",
            hosts=[ModelStopHost(name="local", local=True)],
            claims_dir=str(claims),
            polite_wait_s=2,
            kill_wait_s=2,
            poll_interval_s=0.05,
        )
        contract = yaml.safe_load(
            (
                Path(__file__).resolve().parents[3]
                / "src/omnimarket/nodes/node_remote_lane_stop_effect/contract.yaml"
            ).read_text()
        )
        binding = contract["handler"]
        adapter = getattr(
            importlib.import_module(binding["module"]), binding["class"]
        )()
        result = adapter.handle(request)
        assert result.outcome == "stopped", result.message
        assert child.wait(timeout=5) is not None
        assert not own.exists()
        assert peer.read_bytes() == peer_bytes
        assert result.released_claims == ("example/repo#1",)
        assert "| TERMINAL |" in ledger.read_text()
    finally:
        child.kill()
        child.wait()


def test_final_engine_receipt_releases_claims_without_signalling(
    tmp_path: Path, receipts: Path, ledger: Path, ledger_tool: list[str]
) -> None:
    claims = tmp_path / "claims"
    own = _claim(claims)
    write_receipt(
        receipts,
        host="local",
        status="done",
        final=True,
        session_id=SESSION,
        claim_run_id=RUN_ID,
    )
    request = ModelRemoteLaneStopRequest(
        lane=LANE,
        reason="cleanup after exit",
        stopping_lane="orchestrator-1",
        receipt_dir=str(receipts),
        ledger_path=str(ledger),
        ledger_command=ledger_tool,
        actor="codex:test",
        model="codex",
        claims_dir=str(claims),
    )
    result = create_handler(request).handle(request)
    assert result.outcome == "already-ended", result.message
    assert result.signals == []
    assert not own.exists()
    assert result.released_claims == ("example/repo#1",)
    assert "| TERMINAL |" not in ledger.read_text()


def test_tree_closeout_without_worktrees_still_releases_ended_claims(
    tmp_path: Path,
) -> None:
    claims = tmp_path / "claims"
    own = _claim(claims, claimed_by_run="mutation-run")
    peer = _claim(claims, 2, lane_id="live-peer")
    request = ModelLaneCloseoutRunRequest(
        lane=LANE,
        run_id=RUN_ID,
        lane_dir=str(tmp_path / "no-trees"),
        claims_dir=str(claims),
        session_id=SESSION,
        engine_ended=True,
        claim_run_id="mutation-run",
    )
    result = HandlerLaneCloseoutRun().handle(request)
    assert result.closeouts == ()
    assert not own.exists()
    assert peer.exists()
    assert result.released_claims == ("example/repo#1",)


@pytest.mark.parametrize(
    "mutation", ["session", "same_identity", "same_bytes", "in_place"]
)
def test_replacement_between_inspection_and_release_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    path = _claim(tmp_path)
    original = path.read_bytes()
    rename = Path.rename
    expected: list[bytes] = []

    def race(self: Path, target: Path) -> Path:
        if self == path:
            data = json.loads(original)
            if mutation == "session":
                data["claimed_by_session"] = "replacement-session"
            elif mutation != "same_bytes":
                data["claimed_at"] = "2026-11-04T20:00:01Z"
            content = (
                original if mutation == "same_bytes" else json.dumps(data).encode()
            )
            expected.append(content)
            if mutation == "in_place":
                path.write_bytes(content)
            else:
                replacement = path.with_suffix(".replacement")
                replacement.write_bytes(content)
                replacement.replace(path)
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", race)
    result = _registry(tmp_path)
    assert result.pr_keys == ()
    assert path.read_bytes() == expected[0]


def test_readback_reports_own_replacement_without_deleting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _claim(tmp_path)
    rename = Path.rename

    def race(self: Path, target: Path) -> Path:
        moved = rename(self, target)
        if self == path:
            _claim(tmp_path, claimed_at="2026-11-04T20:00:01Z")
        return moved

    monkeypatch.setattr(Path, "rename", race)
    result = _registry(tmp_path)
    assert result.pr_keys == ("example/repo#1",)
    assert path.exists()
    assert len(result.claims) == 1
    assert result.claims[0].claimed_at == "2026-11-04T20:00:01Z"


@pytest.mark.parametrize(("engine_ended", "session"), [(False, SESSION), (True, None)])
def test_tree_closeout_refuses_unproven_exit_or_missing_session(
    tmp_path: Path, engine_ended: bool, session: str | None
) -> None:
    path = _claim(tmp_path / "claims")
    request = ModelLaneCloseoutRunRequest(
        lane=LANE,
        run_id=RUN_ID,
        lane_dir=str(tmp_path / "no-trees"),
        claims_dir=str(path.parent),
        session_id=session,
        engine_ended=engine_ended,
        claim_run_id=RUN_ID,
    )
    with pytest.raises(ValueError, match=r"(exit|session)"):
        HandlerLaneCloseoutRun().handle(request)
    assert path.exists()


def test_tree_closeout_dry_run_preserves_claims(tmp_path: Path) -> None:
    path = _claim(tmp_path / "claims")
    before = path.read_bytes()
    result = HandlerLaneCloseoutRun().handle(
        ModelLaneCloseoutRunRequest(
            lane=LANE,
            run_id=RUN_ID,
            lane_dir=str(tmp_path / "no-trees"),
            claims_dir=str(path.parent),
            session_id=SESSION,
            engine_ended=True,
            claim_run_id=RUN_ID,
            remove=False,
        )
    )
    assert result.released_claims == ()
    assert path.read_bytes() == before


def test_unlink_failure_restores_the_claim_and_reports_no_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _claim(tmp_path)
    before = path.read_bytes()
    unlink = Path.unlink

    def refuse(self: Path, missing_ok: bool = False) -> None:
        if self.suffix == ".tomb":
            raise PermissionError("fixture refuses removal")
        unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse)
    result = _registry(tmp_path)
    assert result.pr_keys == ()
    assert result.messages
    assert path.read_bytes() == before


def test_final_receipt_without_session_retains_claims(
    tmp_path: Path, receipts: Path, ledger: Path, ledger_tool: list[str]
) -> None:
    path = _claim(tmp_path / "claims")
    write_receipt(receipts, status="done", final=True)
    request = ModelRemoteLaneStopRequest(
        lane=LANE,
        reason="cleanup after exit",
        stopping_lane="orchestrator-1",
        receipt_dir=str(receipts),
        ledger_path=str(ledger),
        ledger_command=ledger_tool,
        actor="codex:test",
        model="codex",
        claims_dir=str(path.parent),
    )
    result = create_handler(request).handle(request)
    assert result.outcome == "refused"
    assert "session identity" in result.message
    assert path.exists()
    assert "| TERMINAL |" not in ledger.read_text()


async def test_closeout_command_releases_claims_through_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    own = _claim(tmp_path / "claims")
    peer = _claim(own.parent, 2, lane_id="live-peer")
    request = ModelLaneCloseoutRunRequest(
        lane=LANE,
        run_id=RUN_ID,
        lane_dir=str(tmp_path / "no-trees"),
        claims_dir=str(own.parent),
        session_id=SESSION,
        engine_ended=True,
        claim_run_id=RUN_ID,
    )
    runtime = await run_over_profile(
        "node_lane_closeout_orchestrator", request, "inmemory", tmp_path, monkeypatch
    )
    assert runtime.handler_result.released_claims == ("example/repo#1",)
    assert not own.exists()
    assert peer.exists()


def test_final_receipt_without_claim_run_retains_claims(
    tmp_path: Path, receipts: Path, ledger: Path, ledger_tool: list[str]
) -> None:
    path = _claim(tmp_path / "claims")
    write_receipt(receipts, status="done", final=True, session_id=SESSION)
    request = ModelRemoteLaneStopRequest(
        lane=LANE,
        reason="cleanup after exit",
        stopping_lane="orchestrator-1",
        receipt_dir=str(receipts),
        ledger_path=str(ledger),
        ledger_command=ledger_tool,
        actor="codex:test",
        model="codex",
        claims_dir=str(path.parent),
    )
    result = create_handler(request).handle(request)
    assert result.outcome == "refused"
    assert "claim run identity" in result.message
    assert path.exists()


def test_tree_closeout_without_claim_run_preserves_claims(tmp_path: Path) -> None:
    path = _claim(tmp_path / "claims")
    request = ModelLaneCloseoutRunRequest(
        lane=LANE,
        run_id=RUN_ID,
        lane_dir=str(tmp_path / "no-trees"),
        claims_dir=str(path.parent),
        session_id=SESSION,
        engine_ended=True,
    )
    with pytest.raises(ValueError, match="run"):
        HandlerLaneCloseoutRun().handle(request)
    assert path.exists()
