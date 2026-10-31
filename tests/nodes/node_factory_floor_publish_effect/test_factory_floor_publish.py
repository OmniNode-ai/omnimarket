# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor publish node: overlay-only host values, atomic remote writes, and the page it ships."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import textwrap
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    NODE_DIR,
    HandlerFactoryFloorPublish,
    PublishRefusedError,
    load_runtime,
    remote_write_command,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishOverlay,
    ModelFactoryFloorPublishRequest,
)
from omnimarket.nodes.node_factory_floor_publish_effect.runtime_factory_floor_publish import (
    main,
)

pytestmark = pytest.mark.unit

RECORD = {
    "schema_id": "factory-floor/1",
    "generated_at": "2026-10-31T12:00:00+00:00",
    "lines": [],
    "floor": {"bays": [], "shipped": [], "waiting": [], "stoppages": []},
    "sources": {},
}


class LocalSsh:
    """Stands in for ssh: runs the remote command with a local shell, so the atomic write really executes."""

    def __init__(self, fail_name: str | None = None) -> None:
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.fail_name = fail_name

    def __call__(
        self, argv: Sequence[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((list(argv), stdin))
        if argv[0] != "ssh":
            return subprocess.CompletedProcess(list(argv), 0, b"", b"")
        remote = argv[-1]
        if self.fail_name and self.fail_name in remote:
            return subprocess.CompletedProcess(list(argv), 1, b"", b"disk full")
        return subprocess.run(
            ["sh", "-c", remote], input=stdin, capture_output=True, check=False
        )


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    target = home / load_runtime().source.relpath
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(RECORD), encoding="utf-8")
    return home


def _overlay(tmp_path: Path) -> ModelFactoryFloorPublishOverlay:
    return ModelFactoryFloorPublishOverlay(
        ssh_target="lab-web",
        remote_dir=str(tmp_path / "site"),
        serve_url="http://example.invalid:8088/",
        bus_lane="lane-x",
    )


def _request(
    tmp_path: Path, state_dir: str | None = None
) -> ModelFactoryFloorPublishRequest:
    return ModelFactoryFloorPublishRequest(
        home=str(_home(tmp_path)),
        overlay=_overlay(tmp_path),
        skip_refresh=True,
        state_dir=state_dir,
    )


def test_overlay_or_missing_key_no_overlay_refuses_and_names_the_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    monkeypatch.delenv("ONEX_FACTORY_FLOOR_PUBLISH_OVERLAY", raising=False)
    request = ModelFactoryFloorPublishRequest(
        home=str(_home(tmp_path)), skip_refresh=True
    )
    ssh = LocalSsh()
    with pytest.raises(NodeOverlayError, match="node_factory_floor_publish_effect"):
        HandlerFactoryFloorPublish(ssh).handle(request, environ={})
    assert ssh.calls == []


def test_overlay_or_missing_key_overlay_without_a_key_refuses(tmp_path: Path) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("ssh_target: lab-web\nremote_dir: /srv/site\n", encoding="utf-8")
    request = ModelFactoryFloorPublishRequest(
        home=str(_home(tmp_path)), skip_refresh=True
    )
    ssh = LocalSsh()
    with pytest.raises(NodeOverlayError):
        HandlerFactoryFloorPublish(ssh).handle(
            request, environ={"ONEX_FACTORY_FLOOR_PUBLISH_OVERLAY": str(overlay)}
        )
    assert ssh.calls == []


def test_overlay_or_missing_key_home_unset_refuses(tmp_path: Path) -> None:
    request = ModelFactoryFloorPublishRequest(
        overlay=_overlay(tmp_path), skip_refresh=True
    )
    with pytest.raises(PublishRefusedError, match="OMNI_HOME is not set"):
        HandlerFactoryFloorPublish(LocalSsh()).handle(request, environ={})


@pytest.mark.parametrize("target", ["-oProxyCommand=x", "a b", "a;b", ""])
def test_overlay_or_missing_key_ssh_target_must_be_a_plain_host(target: str) -> None:
    with pytest.raises(ValueError, match="ssh_target"):
        ModelFactoryFloorPublishOverlay(
            ssh_target=target, remote_dir="/srv/site", serve_url="u"
        )


@pytest.mark.parametrize("path", ["relative/dir", "/srv/../etc"])
def test_overlay_or_missing_key_remote_dir_must_be_absolute(path: str) -> None:
    with pytest.raises(ValueError, match="remote_dir"):
        ModelFactoryFloorPublishOverlay(
            ssh_target="lab-web", remote_dir=path, serve_url="u"
        )


def test_overlay_or_missing_key_handler_source_holds_no_deployment_value() -> None:
    source = (NODE_DIR / "handlers/handler_factory_floor_publish.py").read_text(
        encoding="utf-8"
    )
    assert not re.search(
        r"/(?:Users|home)/|\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b|h0\d\d", source
    )


def test_atomic_publish_writes_both_files_and_leaves_no_temporary(
    tmp_path: Path,
) -> None:
    ssh = LocalSsh()
    result = HandlerFactoryFloorPublish(ssh).handle(
        _request(tmp_path, state_dir=str(tmp_path / "state")), environ={}
    )
    site = tmp_path / "site"
    assert sorted(p.name for p in site.iterdir()) == [
        "factory-floor.json",
        "index.html",
        "publish-status.json",
    ]
    assert (
        json.loads((site / "factory-floor.json").read_text())["generated_at"]
        == RECORD["generated_at"]
    )
    assert (site / "index.html").read_bytes() == (
        NODE_DIR / "page/index.html"
    ).read_bytes()
    assert result.published == (
        "index.html",
        "publish-status.json",
        "factory-floor.json",
    )
    assert [c[0][-1].count("factory-floor.json") > 0 for c in ssh.calls] == [
        False,
        False,
        True,
    ]
    evidence = json.loads((tmp_path / "state/last-run.json").read_text())
    assert evidence["record_generated_at"] == RECORD["generated_at"]
    assert "ssh_target" not in json.dumps(evidence)


def test_atomic_failed_copy_leaves_the_previous_file_untouched(tmp_path: Path) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "factory-floor.json").write_text("previous", encoding="utf-8")
    ssh = LocalSsh(fail_name="factory-floor.json")
    with pytest.raises(
        PublishRefusedError, match="previous file on the server is untouched"
    ):
        HandlerFactoryFloorPublish(ssh).handle(_request(tmp_path), environ={})
    assert (site / "factory-floor.json").read_text() == "previous"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_atomic_real_write_failure_keeps_previous_and_removes_the_temporary(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "factory-floor.json").write_text("previous", encoding="utf-8")
    (site / "index.html").write_text("old page", encoding="utf-8")
    site.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises(PublishRefusedError):
            HandlerFactoryFloorPublish(LocalSsh()).handle(
                _request(tmp_path), environ={}
            )
    finally:
        site.chmod(stat.S_IRWXU)
    assert (site / "factory-floor.json").read_text() == "previous"
    assert (site / "index.html").read_text() == "old page"
    assert [p.name for p in site.iterdir() if p.name.startswith(".")] == []


def test_atomic_remote_command_renames_a_temporary_over_the_final_name() -> None:
    cmd = remote_write_command("/srv/the site", "a b.json")
    assert "mv -f '/srv/the site/.a b.json.tmp' '/srv/the site/a b.json'" in cmd
    assert "rm -f" in cmd


def test_atomic_unreadable_or_foreign_record_is_refused_before_any_copy(
    tmp_path: Path,
) -> None:
    ssh = LocalSsh()
    request = _request(tmp_path)
    record = Path(request.home or "") / load_runtime().source.relpath
    record.write_text('{"hello": 1}', encoding="utf-8")
    with pytest.raises(PublishRefusedError, match="lacks generated_at"):
        HandlerFactoryFloorPublish(ssh).handle(request, environ={})
    record.unlink()
    with pytest.raises(PublishRefusedError, match="unreadable"):
        HandlerFactoryFloorPublish(ssh).handle(request, environ={})
    assert ssh.calls == []


class Scripted(LocalSsh):
    """ssh runs locally; every other command answers from a table keyed by a word of its argv."""

    def __init__(self, answers: dict[str, tuple[int, bytes, bytes]]) -> None:
        super().__init__()
        self.answers = answers

    def __call__(
        self, argv: Sequence[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        if argv[0] == "ssh":
            return super().__call__(argv, stdin, timeout)
        self.calls.append((list(argv), stdin))
        for word, (code, out, err) in self.answers.items():
            if word in argv:
                return subprocess.CompletedProcess(list(argv), code, out, err)
        return subprocess.CompletedProcess(list(argv), 0, b"", b"")


FACTORY = "omnimarket.nodes.node_factory_floor_effect"
HEALTH_MODULE = "omnimarket.nodes.node_process_health_effect.runtime_process_health"
HEALTH = {"generated_at": "2026-10-31T12:00:00Z", "days": [], "summary": {}}


def _refresh_request(tmp_path: Path) -> ModelFactoryFloorPublishRequest:
    return ModelFactoryFloorPublishRequest(
        home=str(_home(tmp_path)), overlay=_overlay(tmp_path)
    )


PLUGIN = {"CLAUDE_PLUGIN_ROOT": "/plugin-root"}


def _program_calls(runner: LocalSsh) -> list[str]:
    return [
        c[0][2]
        if c[0][1] == "-m"
        else "pool"
        if any("onex_lab_run.py" in word for word in c[0])
        else "hosts"
        for c in runner.calls
        if c[0][0] != "ssh"
    ]


def test_refresh_steps_run_in_contract_order_with_the_factory_floor_effect_last(
    tmp_path: Path,
) -> None:
    runner = Scripted({"--json": (0, b"[]", b"")})
    request = _refresh_request(tmp_path)
    HandlerFactoryFloorPublish(runner).handle(request, environ=PLUGIN)
    assert _program_calls(runner) == ["hosts", "pool", HEALTH_MODULE, FACTORY]
    pool = next(c[0] for c in runner.calls if any("onex_lab_run.py" in w for w in c[0]))
    assert pool == [
        "python3",
        "/plugin-root/scripts/onex_lab_run.py",
        "--lane",
        "factory-floor-pool",
        "--hosts",
    ]
    hosts = next(c[0] for c in runner.calls if c[0][0] != "ssh" and c[0][1] != "-m")
    assert hosts[-2:] == ["--bus-lane", "lane-x"]
    assert hosts[0].startswith(request.home or "")
    assert (
        Path(request.home or "") / ".claude_scratch/lab-run/hosts.json"
    ).read_text() == "[]"
    factory = next(c[0] for c in runner.calls if FACTORY in c[0])
    assert factory[-2:] == ["--home", request.home]


def test_refresh_a_required_step_failing_publishes_nothing(tmp_path: Path) -> None:
    runner = Scripted({FACTORY: (3, b"", b"no plans")})
    with pytest.raises(PublishRefusedError, match="required step factory_floor failed"):
        HandlerFactoryFloorPublish(runner).handle(
            _refresh_request(tmp_path), environ={}
        )
    assert not [c for c in runner.calls if c[0][0] == "ssh"]


def test_refresh_an_optional_step_failing_is_published_as_failed_without_its_output(
    tmp_path: Path,
) -> None:
    runner = Scripted({"--json": (1, b"", b"bus: /private/path refused")})
    result = HandlerFactoryFloorPublish(runner).handle(
        _refresh_request(tmp_path), environ=PLUGIN
    )
    failed = {s.name: s for s in result.steps if s.status == "failed"}
    assert set(failed) == {"hosts", "friction"}
    assert failed["hosts"].detail == "exit 1"
    assert "refused" in failed["hosts"].diagnostic
    status = json.loads((tmp_path / "site/publish-status.json").read_text())
    assert {s["name"]: s["status"] for s in status["steps"]}["hosts"] == "failed"
    assert "diagnostic" not in json.dumps(status)
    assert "private" not in json.dumps(status)
    assert status["process_health"] == "absent"


def test_refresh_a_capture_that_is_not_a_json_list_is_a_failed_step(
    tmp_path: Path,
) -> None:
    runner = Scripted({"--json": (0, b'{"a": 1}', b"")})
    result = HandlerFactoryFloorPublish(runner).handle(
        _refresh_request(tmp_path), environ={}
    )
    hosts = next(s for s in result.steps if s.name == "hosts")
    assert (hosts.status, hosts.detail) == ("failed", "output is not a JSON list")
    assert not (tmp_path / "home/.claude_scratch/lab-run/hosts.json").exists()


def test_refresh_the_friction_report_keeps_the_rollup_window_with_its_classes(
    tmp_path: Path,
) -> None:
    request = _refresh_request(tmp_path)
    source = Path(request.home or "") / ".onex_state/friction-rollup/latest.json"
    source.parent.mkdir(parents=True)
    wrapper = {"windows": {"24h": {"classes": [{"class": "lab-idle"}]}, "2h": {}}}
    source.write_text(json.dumps(wrapper), encoding="utf-8")
    result = HandlerFactoryFloorPublish(Scripted({})).handle(request, environ={})
    assert next(s for s in result.steps if s.name == "friction").status == "ok"
    copied = Path(request.home or "") / ".claude_scratch/friction/friction-report.json"
    assert json.loads(copied.read_text()) == wrapper["windows"]["24h"]
    source.write_text('{"windows": {}}', encoding="utf-8")
    missing = HandlerFactoryFloorPublish(Scripted({})).handle(request, environ={})
    friction = next(s for s in missing.steps if s.name == "friction")
    assert (friction.status, friction.detail) == ("failed", "ValueError")
    source.write_text("not json", encoding="utf-8")
    again = HandlerFactoryFloorPublish(Scripted({})).handle(request, environ={})
    assert next(s for s in again.steps if s.name == "friction").status == "failed"


def test_process_health_is_published_beside_the_record_when_it_is_a_series(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    health = Path(request.home or "") / load_runtime().process_health_relpath
    health.parent.mkdir(parents=True)
    health.write_text(json.dumps(HEALTH), encoding="utf-8")
    runner = LocalSsh()
    result = HandlerFactoryFloorPublish(runner).handle(request, environ={})
    assert result.process_health_published is True
    assert result.published == (
        "index.html",
        "publish-status.json",
        "process-health.json",
        "factory-floor.json",
    )
    status = json.loads((tmp_path / "site/publish-status.json").read_text())
    assert status["process_health"] == "published"
    health.write_text('{"generated_at": "x"}', encoding="utf-8")
    again = HandlerFactoryFloorPublish(LocalSsh()).handle(request, environ={})
    assert again.process_health_published is False
    assert "process-health.json" not in again.published


def test_declared_lines_come_from_the_effects_overlay_and_are_published_in_the_status(
    tmp_path: Path,
) -> None:
    overlay = tmp_path / "effect-overlay.yaml"
    overlay.write_text(
        "plan_path_contains:\n  - first-plan\n  - second-plan\nsome_other_field: kept\n",
        encoding="utf-8",
    )
    env = {load_runtime().lines_overlay.file_env: str(overlay)}
    HandlerFactoryFloorPublish(LocalSsh()).handle(_request(tmp_path), environ=env)
    status = json.loads((tmp_path / "site/publish-status.json").read_text())
    assert status["lines_declared"] == {
        "state": "read",
        "plan_path_contains": ["first-plan", "second-plan"],
    }


def test_declared_lines_without_an_overlay_are_published_as_unread_with_no_path(
    tmp_path: Path,
) -> None:
    HandlerFactoryFloorPublish(LocalSsh()).handle(_request(tmp_path), environ={})
    status = json.loads((tmp_path / "site/publish-status.json").read_text())
    assert status["lines_declared"]["state"] == "unread"
    assert "/" not in status["lines_declared"]["reason"]


def test_delegation_counts_are_published_only_when_they_are_a_window_with_outcomes(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    source = Path(request.home or "") / load_runtime().delegation_relpath
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps(
            {
                "generated_at": "2026-10-31T12:00:00Z",
                "outcomes": [{"label": "completed", "count": 3, "tone": "ok"}],
            }
        ),
        encoding="utf-8",
    )
    result = HandlerFactoryFloorPublish(LocalSsh()).handle(request, environ={})
    assert result.delegation_published is True
    assert "delegation.json" in result.published
    source.write_text("[]", encoding="utf-8")
    again = HandlerFactoryFloorPublish(LocalSsh()).handle(request, environ={})
    assert again.delegation_published is False
    assert "delegation.json" not in again.published
    status = json.loads((tmp_path / "site/publish-status.json").read_text())
    assert status["delegation"] == "absent"


def test_cli_refusal_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    monkeypatch.delenv("ONEX_FACTORY_FLOOR_PUBLISH_OVERLAY", raising=False)
    assert main(["--omni-home", str(_home(tmp_path)), "--skip-refresh"]) == 2
    assert "REFUSED factory-floor-publish" in capsys.readouterr().err


PAGE = (NODE_DIR / "page/index.html").read_text(encoding="utf-8")


def test_page_fetches_the_record_and_reads_every_field_the_node_writes() -> None:
    assert 'DATA_FILE = "factory-floor.json"' in PAGE
    runtime = load_runtime()
    assert runtime.names.data == "factory-floor.json"
    assert runtime.names.page == "index.html"
    for field in (
        "generated_at", "lines", "floor", "sources", "line_id", "status_pill", "percent_built", "stations",
        "andon", "flag", "reason", "causes", "items", "feeders", "machines", "lanes", "cloud", "shipped", "waiting",
        "stoppages", "cost_minutes", "occurrences", "recurring", "tickets", "last_seen", "stale",
        "state", "note", "label", "cap", "lanes_placed", "load_per_core", "mem_free_gb", "mem_total_gb",
        "swap_used_pct", "unit_work", "runners", "refusal", "read_age_min", "title", "title_source", "kind",
        "engine", "placement", "status_text", "status_full", "status_at", "status_source", "silent_minutes",
        "stalled", "pr_ci", "pr_ci_note", "pr_title", "pr_state", "started_at", "claim_full", "station",
        "station_id", "item_id", "accounts", "sessions", "lamps", "evidence", "open_sessions", "credit_remaining",
        "pool_read_at", "host_table_read", "placement_markers_read", "receipts_read", "cloud_accounts_read",
        "runner_fleet_read", "stall_minutes", "lane_window_minutes",
        "watcher_last_tick", "ledger_rows", "ledger_newest_row", "linear_issues", "placement_hosts",
        "stoppage_report", "lines_declared", "plan_path_contains", "plan_path", "improved", "worse", "flat",
        "unmeasured", "measured", "publish-status.json", "process-health.json", "reports/manifest.json",
        "delegation.json", "outcomes", "routes", "published_at", "as_of",
    ):  # fmt: skip
        assert re.search(rf"\b{field}\b", PAGE), field


def test_page_carries_no_secret_no_absolute_path_no_hostname_and_no_external_url() -> (
    None
):
    assert not re.search(r"https?://|//[A-Za-z0-9.-]+\.[a-z]{2,}", PAGE)
    assert not re.search(r"/(?:Users|home|Volumes)/", PAGE)
    assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", PAGE)
    assert not re.search(r"\bh0\d\d\b|\.ts\.net|tailscale|ssh", PAGE, re.IGNORECASE)
    assert not re.search(
        r"secret|password|api[_-]?key|bearer|token", PAGE, re.IGNORECASE
    )
    assert PAGE.count("<link") == 1
    assert '<link rel="icon" href="data:,">' in PAGE
    assert "src=" not in PAGE


def test_page_escapes_record_text_before_it_reaches_markup() -> None:
    assert ".replace(/&/g" in PAGE
    assert ".replace(/</g" in PAGE
    assert PAGE.count("${esc(") > 20
    for raw in ("${L.name}", "${s.name}", "${it.title}"):
        assert raw not in PAGE


def test_page_node_syntax_is_valid() -> None:
    script = re.search(r"<script>(.*)</script>", PAGE, re.DOTALL | re.IGNORECASE)
    assert script is not None
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    checked = subprocess.run(
        [node, "--check", "-"],
        input=script.group(1).encode(),
        capture_output=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr.decode()


FLOOR_HARNESS = textwrap.dedent(
    """
    const fs = require("fs");
    const [pagePath, filesJson] = process.argv.slice(1);
    const files = JSON.parse(filesJson);
    const script = /<script>([\\s\\S]*?)<\\/script>/.exec(fs.readFileSync(pagePath, "utf8"))[1];
    const els = {};
    const mk = id => ({id, innerHTML: "", textContent: "", hidden: true, dataset: {}, style: {},
      querySelectorAll: () => [], setAttribute() {}, addEventListener() {}, replaceChildren() {}});
    globalThis.document = {getElementById: id => els[id] || (els[id] = mk(id)), addEventListener() {},
      querySelectorAll: () => [], createElement: () => mk("created")};
    globalThis.getComputedStyle = () => ({getPropertyValue: () => "1"});
    globalThis.setInterval = () => 0;
    globalThis.fetch = async url => {
      const key = Object.keys(files).find(k => url === k);
      if (!key) return {ok: false, status: 404, json: async () => ({})};
      return {ok: true, status: 200, json: async () => JSON.parse(files[key])};
    };
    (async () => {
      new Function(script).call(globalThis);
      await new Promise(r => setTimeout(r, 150));
      const out = {};
      for (const [k, v] of Object.entries(els)) out[k] = v.innerHTML + "|" + v.textContent;
      console.log(JSON.stringify(out));
    })();
    """
)

FLOOR_RECORD = {
    "schema_id": "factory-floor/1",
    "generated_at": "2099-01-01T00:00:00Z",
    "lines": [
        {
            "line_id": "plan-two",
            "name": "Second plan",
            "plan_path": "beta/plans/two-plan.md",
            "source": "manifest",
            "status_pill": "stopped",
            "percent_built": 37,
            "andon": {"flag": True, "reason": "gate failed", "causes": ["G9"]},
            "stations": [
                {"id": "0", "name": "Look first", "state": "done", "feeders": [],
                 "items": [{"id": "a", "title": "Part A", "kind": "lane", "state": "done", "note": "merged", "tickets": [], "prs": []}]},
                {"id": "1", "name": "Then build", "state": "stop", "feeders": ["0"],
                 "items": [{"id": "b", "title": "Part B", "kind": "gate", "state": "stop", "note": "", "tickets": [], "prs": []}]},
            ],
        }
    ],
    "floor": {
        "bays": [],
        "shipped": [{"pr": "repo#1", "title": "Shipped one", "line_id": "plan-two", "detail": "merged"}],
        "waiting": [],
        "stoppages": [{"name": "lab-idle", "occurrences": 7, "cost_minutes": 4321.0, "recurring": True, "tickets": [], "last_seen": "x"}],
    },
    "sources": {"watcher_last_tick": "2099-01-01T00:00:00Z", "ledger_rows": 7311, "ledger_newest_row": "", "linear_issues": 0, "placement_hosts": 0, "stoppage_report": True},
}  # fmt: skip


def _run_floor(files: dict[str, object]) -> dict[str, str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    done = subprocess.run(
        [
            node,
            "-e",
            FLOOR_HARNESS,
            str(NODE_DIR / "page/index.html"),
            json.dumps({k: json.dumps(v) for k, v in files.items()}),
        ],
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr.decode()
    return cast(
        "dict[str, str]", json.loads(done.stdout.decode().strip().splitlines()[-1])
    )


def test_page_draws_the_declared_lines_in_order_with_an_unmatched_one_unread() -> None:
    declared = {
        "state": "read",
        "plan_path_contains": ["first-plan", "two-plan", "third-plan"],
    }
    out = _run_floor(
        {
            "factory-floor.json": FLOOR_RECORD,
            "publish-status.json": {
                "published_at": "2099-01-01T00:00:00Z",
                "steps": [],
                "lines_declared": declared,
            },
        }
    )
    lines = out["lines"]
    assert lines.index("Line A") < lines.index("Line B") < lines.index("Line C")
    assert "No plan read for this line" in lines
    assert "first-plan" in lines
    assert "third-plan" in lines
    assert "Frame finished" in lines
    assert "Cord pulled: gate failed" in lines
    assert "37% of the car built" in lines
    assert "Line C · not in the record" in out["andon"]


def test_page_shows_each_number_from_the_record_and_never_a_baked_one() -> None:
    out = _run_floor({"factory-floor.json": FLOOR_RECORD})
    assert "7,311" in out["tally"]
    assert "Machines · unread" in out["andon"]
    assert "lab-idle" in out["stopChart"]
    assert "4,321" in out["stopChart"]
    assert "Unread" in out["delegBody"]
    for baked in ("251 of 251", "287 completed", "456 delegation"):
        assert baked not in PAGE


def lane_cells(**cells: object) -> dict[str, object]:
    base: dict[str, object] = {
        "lane": "lane-one",
        "ticket": "OMN-9",
        "title": "Show every lane on the floor",
        "title_source": "linear",
        "kind": "fix",
        "engine": "claude",
        "placement": "machine",
        "host": "bay-x",
        "cloud_account": "",
        "started_at": "2098-12-31T23:00:00Z",
        "run_minutes": 60,
        "status_text": "pushed the effect fields",
        "status_full": "pushed the effect fields; waiting on CI for the long full row",
        "status_at": "2098-12-31T23:50:00Z",
        "status_age_min": 10,
        "status_source": "status",
        "silent_minutes": 10,
        "stalled": False,
        "pr": "repo#7",
        "pr_title": "feat: lanes",
        "pr_state": "OPEN",
        "pr_ci": "red",
        "pr_ci_note": "2 red of 150",
        "station": {"line_id": "plan-two", "station_id": "1", "item_id": "b"},
        "claim_full": "Fix the reds on the floor stack",
    }
    return {**base, **cells}


def machine_cells(**cells: object) -> dict[str, object]:
    base: dict[str, object] = {
        "host": "bay-x",
        "label": "bay-x",
        "state": "read",
        "note": "",
        "read_at": "2098-12-31T23:58:00Z",
        "read_age_min": 2,
        "stale": False,
        "cores": 8,
        "load1": 4.0,
        "load_per_core": 0.5,
        "busy_cores": 3.0,
        "mem_free_gb": 12.0,
        "mem_total_gb": 32.0,
        "swap_used_pct": None,
        "cap": 4,
        "refusal": "",
        "lanes_placed": 2,
        "lanes": ["lane-one", "lane-two"],
        "units_placed": 3,
        "unit_work": 1,
        "runners": {
            "state": "unread",
            "total": None,
            "online": None,
            "busy": None,
            "observed_at": "",
            "note": "no runner fleet observation has been recorded",
        },
    }
    return {**base, **cells}


BAYS_RECORD = {
    **FLOOR_RECORD,
    "floor": {
        **FLOOR_RECORD["floor"],
        "machines": [
            machine_cells(),
            machine_cells(host="bay-y", label="bay-y", state="unread", note="rc=255 timed out after 20s", lanes=[], lanes_placed=0, cap=None, load_per_core=None, mem_free_gb=None, mem_total_gb=None),
            machine_cells(host="bay-z", label="bay-z", state="repair", note="down for repair", lanes=[], lanes_placed=0),
        ],
        "stall_minutes": 45,
        "lane_window_minutes": 180,
        "lanes": [
            lane_cells(),
            lane_cells(lane="lane-two", ticket="", title="", title_source="unread", engine="codex", status_text="", status_full="", status_source="claim", stalled=True, silent_minutes=90, pr="", pr_ci="", pr_state="", station=None),
            lane_cells(lane="lane-lost", host="", placement="unplaced"),
            lane_cells(lane="lane-cloud", host="", placement="cloud", cloud_account="acct-a"),
        ],
        "cloud": {
            "state": "read",
            "note": "",
            "accounts": [{"name": "acct-a", "ready": True, "login": "ok", "cap": 14, "open_sessions": 1, "credit_remaining": 244.0, "credit_read_at": "2098-12-30T00:00:00Z", "rate_status": "allowed", "rate_resets_at": ""}],
            "sessions": [{"lane": "lane-cloud", "run_id": "r1", "account": "acct-a", "started_at": "2098-12-31T23:00:00Z", "run_minutes": 60, "status": "running", "open": True, "result": ""}],
            "lamps": [{"id": "cloud-no-push", "text": "Cloud sessions are ending without a pull request", "since": "2098-12-31T12:00:00Z", "evidence": "push refused (403)"}],
        },
    },
}  # fmt: skip


def test_page_draws_a_bay_per_machine_with_its_lanes_against_the_cap() -> None:
    out = _run_floor({"factory-floor.json": BAYS_RECORD})
    grid = out["bayGrid"]
    assert "bay-x" in grid
    assert '2</span> lanes placed of a cap of <span class="mono">4' in grid
    assert grid.count('class="cap"') >= 1
    assert "0.5" in grid
    assert "12 GB of 32" in grid
    assert "swap used" in grid
    assert "unit work" in grid
    assert "1 placed beside the lanes" in grid
    assert "CI runners" in grid


def test_page_states_the_stall_and_window_the_record_judged_lanes_by() -> None:
    note = _run_floor({"factory-floor.json": BAYS_RECORD})["baysNote"]
    assert "stalled after 45 minutes" in note
    assert "no longer drawn after 180 minutes" in note


def test_page_draws_a_machine_the_pool_could_not_read_as_unread_and_one_in_repair_as_repair() -> (
    None
):
    grid = _run_floor({"factory-floor.json": BAYS_RECORD})["bayGrid"]
    assert "mbay unread" in grid
    assert "rc=255 timed out after 20s" in grid
    assert "mbay repair" in grid
    assert "down for repair" in grid
    assert "Nothing is drawn" in grid


def test_page_draws_a_lane_with_title_kind_engine_status_pr_ci_and_a_station_link() -> (
    None
):
    grid = _run_floor({"factory-floor.json": BAYS_RECORD})["bayGrid"]
    assert "Show every lane on the floor" in grid
    assert "OMN-9" in grid
    assert '<span class="chip">fix</span>' in grid
    assert "Claude" in grid
    assert "pushed the effect fields" in grid
    assert "repo#7 · CI red" in grid
    assert "pushed the effect fields; waiting on CI for the long full row" in grid
    assert 'data-jump="st-plan-two-1"' in grid
    assert "Line A · Then build · Part B" in grid
    assert (
        'title="OMN-9 · Show every lane on the floor · pushed the effect fields; waiting'
        in grid
    )


def test_page_marks_a_stalled_lane_and_a_lane_with_no_title_or_status_as_unread() -> (
    None
):
    grid = _run_floor({"factory-floor.json": BAYS_RECORD})["bayGrid"]
    assert "chip stall" in grid
    assert "stalled · silent 90 min" in grid
    assert "ticket title unread" in grid
    assert "no status written" in grid
    assert "no PR yet" in grid
    assert "no station on the lines holds this ticket" in grid


def test_page_puts_a_lane_with_no_machine_in_its_own_bay_and_a_cloud_lane_in_the_cloud_bay() -> (
    None
):
    grid = _run_floor({"factory-floor.json": BAYS_RECORD})["bayGrid"]
    unplaced = grid[grid.index('id="bay-unplaced"') :]
    assert "lane-lost" in unplaced
    cloud = grid[grid.index('id="bay-cloud"') : grid.index('id="bay-unplaced"')]
    assert "lane-cloud" in cloud
    assert "acct-a" in cloud
    assert "244" in cloud
    assert "Cloud sessions are ending without a pull request" in cloud
    assert "push refused (403)" in cloud


def test_page_lights_the_andon_lamps_for_machines_lanes_and_the_cloud_stop() -> None:
    andon = _run_floor({"factory-floor.json": BAYS_RECORD})["andon"]
    assert "Machines · 2 lanes placed" in andon
    assert "1 of 3 read" in andon
    assert "1 unread, 1 in repair" in andon
    assert "Lanes · 4 live" in andon
    assert "1 stalled" in andon
    assert "Cloud · stop" in andon


def test_page_shows_the_part_of_the_car_a_lane_builds_with_a_link_back_to_the_lane() -> (
    None
):
    lines = _run_floor({"factory-floor.json": BAYS_RECORD})["lines"]
    assert 'id="st-plan-two-1"' in lines
    assert 'id="part-plan-two-1-b"' in lines
    assert 'Built by <a data-jump="lane-lane-one"' in lines


def test_page_without_machine_fields_says_the_bays_are_unread_never_empty() -> None:
    out = _run_floor({"factory-floor.json": FLOOR_RECORD})
    assert "declares no machine bays" in out["baysNote"]
    assert "Machines · unread" in out["andon"]


def test_page_draws_the_delegation_card_from_its_file() -> None:
    delegation = {
        "generated_at": "2099-01-01T00:00:00Z",
        "window": {"start": "2098-12-31T19:12:00Z", "end": "2099-01-01T07:12:00Z"},
        "total": 11,
        "outcomes": [
            {"label": "completed", "count": 9, "tone": "ok"},
            {"label": "timed out", "count": 2, "tone": "warn"},
        ],
        "routes": [{"label": "Local lab model", "done": 9, "total": 9}],
        "why": "A reason the file states.",
    }
    out = _run_floor(
        {"factory-floor.json": FLOOR_RECORD, "delegation.json": delegation}
    )
    assert "9 completed" in out["delegBody"]
    assert "9 of 9" in out["delegBody"]
    assert "A reason the file states." in out["delegBody"]
