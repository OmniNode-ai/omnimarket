# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_branch_prune_effect: fakes only, no GitHub call, no real branch (OMN-21890)."""

from __future__ import annotations

import datetime as dt
import importlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.nodes.node_branch_prune_effect.handlers.handler_branch_prune import (
    HandlerBranchPrune,
)
from omnimarket.nodes.node_branch_prune_effect.models import (
    EnumBranchAction,
    EnumBranchPruneVerdict,
    EnumPullRequestState,
    ModelBranchInfo,
    ModelBranchPrunePolicy,
    ModelBranchPruneRequest,
    ModelPullRequestInfo,
)
from omnimarket.nodes.node_branch_prune_effect.node import NodeBranchPruneEffect
from omnimarket.nodes.node_branch_prune_effect.runtime_branch_prune_cli import (
    main as run_once,
)

NODE_DIR = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_branch_prune_effect"
)
NOW = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)
OLD = NOW - dt.timedelta(days=60)
REPO = "acme/widgets"


def branch(name: str, sha: str, committed: dt.datetime = OLD) -> ModelBranchInfo:
    return ModelBranchInfo(name=name, tip_sha=sha, tip_committed_at=committed)


def pull(
    n: int, ref: str, sha: str, state: EnumPullRequestState
) -> ModelPullRequestInfo:
    return ModelPullRequestInfo(number=n, head_ref=ref, head_sha=sha, state=state)


class FakeHost:
    def __init__(
        self,
        branches: list[ModelBranchInfo],
        pulls: list[ModelPullRequestInfo],
        events: list[tuple[str, ...]] | None = None,
        default: str = "main",
    ) -> None:
        self.branches = branches
        self.pulls = pulls
        self.default = default
        self.events = events if events is not None else []
        self.delete_calls: list[tuple[str, str, str]] = []

    def default_branch(self, repository: str) -> str:
        return self.default

    def list_branches(self, repository: str) -> list[ModelBranchInfo]:
        return list(self.branches)

    def list_pull_requests(self, repository: str) -> list[ModelPullRequestInfo]:
        return list(self.pulls)

    def delete_branch(self, repository: str, branch: str, *, expected_sha: str) -> None:
        self.events.append(("delete", branch, expected_sha))
        self.delete_calls.append((repository, branch, expected_sha))


class FakeTips:
    def __init__(
        self, events: list[tuple[str, ...]] | None = None, fail: bool = False
    ) -> None:
        self.events = events if events is not None else []
        self.fail = fail
        self.rows: list[tuple[str, str, str]] = []

    def record(self, repository: str, branch: str, tip_sha: str) -> None:
        self.events.append(("record", branch, tip_sha))
        if self.fail:
            raise OSError("disk full")
        self.rows.append((repository, branch, tip_sha))


def policy(**kw: Any) -> ModelBranchPrunePolicy:
    base: dict[str, Any] = {
        "repositories": (REPO,),
        "protected_names": ("release",),
        "protected_patterns": ("hotfix/*",),
        "min_age_days": 14,
    }
    base.update(kw)
    return ModelBranchPrunePolicy(**base)


def handler(
    host: FakeHost, tips: FakeTips, pol: ModelBranchPrunePolicy | None = None
) -> HandlerBranchPrune:
    return HandlerBranchPrune(host=host, tips=tips, policy=pol or policy(), now=NOW)


def standard_host(events: list[tuple[str, ...]] | None = None) -> FakeHost:
    return FakeHost(
        branches=[
            branch("main", "m1"),
            branch("feat/merged", "a1"),
            branch("feat/closed", "b1"),
            branch("feat/open", "c1"),
            branch("release", "d1"),
            branch("hotfix/x", "e1"),
            branch("feat/stray", "f1"),
        ],
        pulls=[
            pull(1, "feat/merged", "a1", EnumPullRequestState.MERGED),
            pull(2, "feat/closed", "b1", EnumPullRequestState.CLOSED),
            pull(3, "feat/open", "c1", EnumPullRequestState.OPEN),
            pull(4, "release", "d1", EnumPullRequestState.MERGED),
            pull(5, "hotfix/x", "e1", EnumPullRequestState.MERGED),
        ],
        events=events,
    )


# --- AC1 ----------------------------------------------------------------------


def _check_contract_binding(raw: dict[str, Any]) -> None:
    binding = raw["handler"]
    module = importlib.import_module(binding["module"])
    handler_cls = getattr(module, binding["class"])
    assert handler_cls is HandlerBranchPrune
    routes = raw["handler_routing"]["handlers"]
    assert routes, "no handler is routed from the contract"
    for route in routes:
        routed = getattr(
            importlib.import_module(route["handler"]["module"]),
            route["handler"]["name"],
        )
        assert routed is HandlerBranchPrune


def _contract() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    return data


def test_branch_prune_contract_binds_the_handler() -> None:
    raw = _contract()
    assert raw["node_type"] == "EFFECT_GENERIC"
    _check_contract_binding(raw)
    assert (NODE_DIR / "node.py").is_file()
    assert (NODE_DIR / "metadata.yaml").is_file()


def test_branch_prune_contract_fails_when_the_binding_is_removed() -> None:
    raw = _contract()
    del raw["handler"]
    with pytest.raises(KeyError):
        _check_contract_binding(raw)
    raw = _contract()
    raw["handler_routing"]["handlers"] = []
    with pytest.raises(AssertionError):
        _check_contract_binding(raw)


# --- AC2 ----------------------------------------------------------------------

SEEDED_NAMES = ["acme/widgets", "Ada Lovelace"]
SOURCE_NAMES = ["Ada Lovelace", "Grace Hopper", "example-org/example-repo"]


def _hits(root: Path, needles: list[str]) -> list[str]:
    return [
        f"{p.name}:{n}"
        for p in sorted(root.rglob("*.py"))
        for n in needles
        if n in p.read_text(encoding="utf-8")
    ]


def test_branch_prune_overlay_no_repo_or_person_name_in_source(tmp_path: Path) -> None:
    assert _hits(NODE_DIR, SOURCE_NAMES + SEEDED_NAMES) == []
    # positive control: the grep fires when a seeded name is planted
    planted = tmp_path / "planted.py"
    planted.write_text('REPO = "acme/widgets"  # Ada Lovelace\n')
    assert _hits(tmp_path, SEEDED_NAMES) == [
        "planted.py:acme/widgets",
        "planted.py:Ada Lovelace",
    ]


def _overlay_env(tmp_path: Path, doc: dict[str, Any]) -> dict[str, str]:
    node_dir = tmp_path / "overlays" / "node_branch_prune_effect"
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "overlay.yaml").write_text(yaml.safe_dump(doc))
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays")}


def test_branch_prune_overlay_change_changes_the_keep_decision(tmp_path: Path) -> None:
    doc: dict[str, Any] = {
        "repositories": [REPO],
        "protected_names": ["release"],
        "protected_patterns": ["hotfix/*"],
        "min_age_days": 14,
    }
    env = _overlay_env(tmp_path, doc)

    def run() -> dict[str, EnumBranchAction]:
        h = HandlerBranchPrune(
            host=standard_host(), tips=FakeTips(), environ=env, now=NOW
        )
        r = h.handle(ModelBranchPruneRequest())
        return {d.branch: d.action for d in r.decisions}

    first = run()
    assert first["feat/merged"] == EnumBranchAction.WOULD_DELETE
    doc["protected_patterns"] = ["hotfix/*", "feat/*"]
    _overlay_env(tmp_path, doc)
    second = run()
    assert second["feat/merged"] == EnumBranchAction.KEEP
    doc["protected_patterns"] = ["hotfix/*"]
    doc["min_age_days"] = 90
    _overlay_env(tmp_path, doc)
    assert run()["feat/merged"] == EnumBranchAction.KEEP
    doc["min_age_days"] = 14
    doc["repositories"] = []
    _overlay_env(tmp_path, doc)
    assert run() == {}


def test_branch_prune_overlay_absent_or_invalid_refuses(tmp_path: Path) -> None:
    host = standard_host()
    h = HandlerBranchPrune(host=host, tips=FakeTips(), environ={}, now=NOW)
    r = h.handle(ModelBranchPruneRequest(dry_run=False))
    assert r.verdict == EnumBranchPruneVerdict.REFUSED
    assert "ONEX_SKILL_OVERLAY_ROOTS" in r.detail
    env = _overlay_env(tmp_path, {"repositories": [REPO], "surprise": 1})
    r = HandlerBranchPrune(host=host, tips=FakeTips(), environ=env).handle(
        ModelBranchPruneRequest(dry_run=False)
    )
    assert r.verdict == EnumBranchPruneVerdict.REFUSED
    assert host.delete_calls == []


# --- AC3 ----------------------------------------------------------------------


def test_branch_prune_sha_precise_recreated_branch_is_kept() -> None:
    host = FakeHost(
        branches=[branch("feat/again", "new-tip")],
        pulls=[pull(9, "feat/again", "old-tip", EnumPullRequestState.MERGED)],
    )
    r = handler(host, FakeTips()).handle(ModelBranchPruneRequest(dry_run=False))
    assert host.delete_calls == []
    (d,) = r.decisions
    assert d.action == EnumBranchAction.KEEP
    assert "differs" in d.reason


def test_branch_prune_sha_precise_deletes_merged_and_closed_tips() -> None:
    host = standard_host()
    r = handler(host, FakeTips()).handle(ModelBranchPruneRequest(dry_run=False))
    assert r.verdict == EnumBranchPruneVerdict.PRUNED
    assert sorted(c[1] for c in host.delete_calls) == ["feat/closed", "feat/merged"]
    assert {c[1]: c[2] for c in host.delete_calls} == {
        "feat/closed": "b1",
        "feat/merged": "a1",
    }


def test_branch_prune_sha_precise_branch_without_pull_request_is_kept() -> None:
    host = standard_host()
    r = handler(host, FakeTips()).handle(ModelBranchPruneRequest(dry_run=False))
    stray = next(d for d in r.decisions if d.branch == "feat/stray")
    assert stray.action == EnumBranchAction.KEEP


# --- AC4 ----------------------------------------------------------------------


def test_branch_prune_tip_record_precedes_each_delete() -> None:
    events: list[tuple[str, ...]] = []
    host = standard_host(events)
    tips = FakeTips(events)
    handler(host, tips).handle(ModelBranchPruneRequest(dry_run=False))
    assert events
    for i, ev in enumerate(events):
        if ev[0] == "delete":
            assert events[i - 1] == ("record", ev[1], ev[2])
    assert [e[0] for e in events] == ["record", "delete"] * 2
    assert sorted(tips.rows) == [
        (REPO, "feat/closed", "b1"),
        (REPO, "feat/merged", "a1"),
    ]


def test_branch_prune_tip_record_failed_write_prevents_the_delete() -> None:
    events: list[tuple[str, ...]] = []
    host = standard_host(events)
    tips = FakeTips(events, fail=True)
    r = handler(host, tips).handle(ModelBranchPruneRequest(dry_run=False))
    assert host.delete_calls == []
    assert [e[0] for e in events] == ["record", "record"]
    assert r.verdict == EnumBranchPruneVerdict.FAILED
    failed = [d for d in r.decisions if d.action == EnumBranchAction.RECORD_FAILED]
    assert len(failed) == 2


# --- AC5 ----------------------------------------------------------------------


def test_branch_prune_dry_run_is_default_and_issues_no_delete() -> None:
    assert ModelBranchPruneRequest().dry_run is True
    host = standard_host()
    tips = FakeTips()
    r = handler(host, tips).handle(ModelBranchPruneRequest())
    assert host.delete_calls == []
    assert tips.rows == []
    assert r.verdict == EnumBranchPruneVerdict.DRY_RUN
    assert {(d.branch, d.reason) for d in r.candidates} == {
        ("feat/merged", "tip equals head of merged pull request #1"),
        ("feat/closed", "tip equals head of closed pull request #2"),
    }
    assert all(d.action == EnumBranchAction.WOULD_DELETE for d in r.candidates)


# --- AC6 ----------------------------------------------------------------------


@pytest.mark.parametrize("age_days", [0, 14, 3650])
def test_branch_prune_never_delete_protected_default_or_open(age_days: int) -> None:
    host = standard_host()
    host.pulls.append(pull(6, "main", "m1", EnumPullRequestState.MERGED))
    # an open pull request and a merged one on the same name: open wins
    host.pulls.append(pull(7, "feat/merged", "zz", EnumPullRequestState.OPEN))
    h = handler(host, FakeTips(), policy(min_age_days=age_days))
    r = h.handle(ModelBranchPruneRequest(dry_run=False))
    deleted = {c[1] for c in host.delete_calls}
    assert deleted.isdisjoint({"main", "release", "hotfix/x", "feat/open"})
    assert "feat/merged" not in deleted
    reasons = {d.branch: d.reason for d in r.decisions}
    assert reasons["main"] == "default branch"
    assert reasons["release"] == "protected by the keep policy"
    assert reasons["hotfix/x"] == "protected by the keep policy"
    assert reasons["feat/open"] == "open pull request"


def test_branch_prune_never_delete_age_floor_keeps_young_branches() -> None:
    host = FakeHost(
        branches=[branch("feat/new", "n1", committed=NOW - dt.timedelta(days=1))],
        pulls=[pull(1, "feat/new", "n1", EnumPullRequestState.MERGED)],
    )
    handler(host, FakeTips()).handle(ModelBranchPruneRequest(dry_run=False))
    assert host.delete_calls == []


# --- AC7 ----------------------------------------------------------------------


def _tick() -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=NOW,
        tick_id="00000000-0000-0000-0000-000000000001",
        sequence_number=1,
        scheduled_at=NOW,
        correlation_id="00000000-0000-0000-0000-000000000002",
        scheduler_id="test",
        tick_interval_ms=1000,
    )


def test_branch_prune_schedule_event_runs_end_to_end_dry_by_default() -> None:
    host = standard_host()
    tips = FakeTips()
    node = NodeBranchPruneEffect(host, tips)
    node.handler = handler(host, tips)
    result = node.handler.handle(_tick())
    assert result.verdict == EnumBranchPruneVerdict.DRY_RUN
    assert host.delete_calls == []
    assert len(result.candidates) == 2


def test_branch_prune_schedule_event_deletes_when_overlay_allows() -> None:
    host = standard_host()
    node = NodeBranchPruneEffect(host, FakeTips())
    node.handler = handler(host, FakeTips(), policy(scheduled_dry_run=False))
    result = node.handler.handle(_tick())
    assert result.verdict == EnumBranchPruneVerdict.PRUNED
    assert len(host.delete_calls) == 2


def test_branch_prune_schedule_event_run_command_dry_run_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = {"repositories": [REPO], "protected_names": ["release"], "min_age_days": 14}
    env = _overlay_env(tmp_path, doc)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", env["ONEX_SKILL_OVERLAY_ROOTS"])
    fixture = {
        REPO: {
            "default": "main",
            "branches": [
                {
                    "name": "feat/merged",
                    "tip_sha": "a1",
                    "tip_committed_at": OLD.isoformat(),
                },
                {"name": "main", "tip_sha": "m1", "tip_committed_at": OLD.isoformat()},
            ],
            "pull_requests": [
                {
                    "number": 1,
                    "head_ref": "feat/merged",
                    "head_sha": "a1",
                    "state": "merged",
                }
            ],
        }
    }
    path = tmp_path / "host.json"
    path.write_text(json.dumps(fixture))
    assert run_once(["--fixture", str(path), "--dry-run"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["verdict"] == "dry_run"
    assert report["decisions"][0]["action"] == "would_delete"
    assert json.loads(path.read_text()) == fixture  # nothing was deleted
    # positive control: --execute deletes from the fixture host only
    tips = tmp_path / "tips.jsonl"
    assert (
        run_once(["--fixture", str(path), "--execute", "--tips-file", str(tips)]) == 0
    )
    assert json.loads(tips.read_text()) == [REPO, "feat/merged", "a1"]
    capsys.readouterr()


def test_branch_prune_schedule_event_run_command_without_overlay_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    path = tmp_path / "host.json"
    path.write_text("{}")
    assert run_once(["--fixture", str(path)]) == 1
    assert json.loads(capsys.readouterr().out)["verdict"] == "refused"


def test_branch_prune_contract_declares_the_completed_topic() -> None:
    completed = "onex.evt.omnimarket.branch-prune-completed.v1"
    raw = _contract()
    assert raw["terminal_event"] == completed
    assert raw["event_bus"]["publish_topics"] == [completed]
    assert raw["published_events"][0]["topic"] == completed


def test_branch_prune_deploy_registers_the_node_and_console_script() -> None:
    import tomllib

    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    )
    scripts = pyproject["project"]["scripts"]
    nodes = pyproject["project"]["entry-points"]["onex.nodes"]
    assert (
        scripts["onex-branch-prune"]
        == "omnimarket.nodes.node_branch_prune_effect.runtime_branch_prune_cli:main"
    )
    assert nodes["node_branch_prune_effect"] == (
        "omnimarket.nodes.node_branch_prune_effect"
    )
    module = importlib.import_module(
        "omnimarket.nodes.node_branch_prune_effect.runtime_branch_prune_cli"
    )
    assert callable(module.main)
