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
        "floor",
        "index.html",
        "publish-status.json",
    ]
    assert [p.name for p in (site / "floor").iterdir()] == ["index.html"]
    assert (
        json.loads((site / "factory-floor.json").read_text())["generated_at"]
        == RECORD["generated_at"]
    )
    assert (site / "index.html").read_bytes() == (
        NODE_DIR / "page/index.html"
    ).read_bytes()
    assert (site / "floor/index.html").read_bytes() == (
        NODE_DIR / "page/floor/index.html"
    ).read_bytes()
    assert result.published == (
        "floor/index.html",
        "index.html",
        "publish-status.json",
        "factory-floor.json",
    )
    assert [c[0][-1].count("factory-floor.json") > 0 for c in ssh.calls] == [
        False,
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
        "floor/index.html",
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


PAGE = (NODE_DIR / "page/floor/index.html").read_text(encoding="utf-8")
SHELL = (NODE_DIR / "page/index.html").read_text(encoding="utf-8")


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
        "unmeasured", "measured", "publish-status.json", "process-health.json",
        "delegation.json", "outcomes", "routes", "published_at",
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
      const key = Object.keys(files).find(k => url === "../" + k);
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
            str(NODE_DIR / "page/floor/index.html"),
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
            machine_cells(host="bay-w", label="bay-w", state="out", note="placement paused", lanes=[], lanes_placed=0),
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
    assert "out of selection" in grid
    assert "placement paused" in grid
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
    assert "1 of 4 read" in andon
    assert "1 unread, 2 in repair or out" in andon
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


# ---- The tab page (OMN-21733): a dashboard and four tabs on one page.

SHELL_HARNESS = textwrap.dedent(
    """
    const fs = require("fs");
    const [pagePath, filesJson, optsJson] = process.argv.slice(1);
    const files = JSON.parse(filesJson);
    const opts = JSON.parse(optsJson);
    const script = /<script>([\\s\\S]*?)<\\/script>/.exec(fs.readFileSync(pagePath, "utf8"))[1];
    const els = {};
    const mk = id => ({id, innerHTML: "", textContent: "", hidden: false, dataset: {}, style: {}, className: "",
      tabIndex: 0, title: "", children: [], attrs: {}, classList: {add() {}, remove() {}, toggle() {}},
      querySelectorAll: () => [], setAttribute(k, v) { this.attrs[k] = v; }, addEventListener() {},
      appendChild(c) { this.children.push(c); }, focus() {}});
    const saved = {};
    globalThis.document = {title: "", getElementById: id => els[id] || (els[id] = mk(id)), createElement: () => mk("created"), addEventListener() {}};
    globalThis.window = {addEventListener() {}, matchMedia: () => ({matches: false})};
    globalThis.location = {hash: opts.hash || "", search: "", origin: "null"};
    globalThis.history = {replaceState(_a, _b, h) { globalThis.location.hash = h; }, pushState(_a, _b, h) { globalThis.location.hash = h; }};
    globalThis.localStorage = opts.storeBlocked
      ? {getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); }}
      : {getItem: k => (opts.stored && k === "factory-floor.tab" ? opts.stored : null), setItem(k, v) { saved[k] = v; }};
    globalThis.setInterval = () => 0;
    globalThis.fetch = async url => {
      if (!(url in files)) return {ok: false, status: 404, json: async () => ({})};
      return {ok: true, status: 200, json: async () => JSON.parse(files[url])};
    };
    (async () => {
      new Function(script).call(globalThis);
      await new Promise(r => setTimeout(r, 150));
      const out = {};
      for (const [k, v] of Object.entries(els)) out[k] = v.innerHTML + "|" + v.textContent;
      const sel = Object.keys(els).filter(k => k.startsWith("tab-") && els[k].attrs["aria-selected"] === "true");
      const shown = Object.keys(els).filter(k => k.startsWith("pane-") && !els[k].hidden);
      const frames = {};
      for (const k of Object.keys(els)) if (k.startsWith("pane-")) frames[k] = els[k].children.map(c => c.src);
      console.log(JSON.stringify({out, sel, shown, frames, hash: globalThis.location.hash, saved, title: globalThis.document.title}));
    })();
    """
)

SHELL_NOW = "2099-01-01T00:10:00Z"
SHELL_RECORD = {
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
            "andon": {"flag": True, "reason": "PR repo#9 CI red (2 red of 10)", "causes": ["PR repo#9 CI red (2 red of 10)"]},
            "stations": [
                {"id": "0", "name": "Look first", "state": "done", "feeders": [],
                 "items": [{"id": "a", "title": "Part A", "kind": "lane", "state": "done", "note": "", "tickets": [], "prs": []},
                           {"id": "b", "title": "Part B", "kind": "lane", "state": "stop", "note": "", "tickets": [], "prs": []}]},
            ],
        }
    ],
    "floor": {
        "machines": [
            {"host": "m1", "label": "Bay one", "state": "read", "note": "", "read_at": "2099-01-01T00:00:00Z", "read_age_min": 0,
             "stale": False, "cores": 8, "load1": 4.0, "load_per_core": 0.5, "busy_cores": 4.0, "mem_free_gb": 12.5,
             "mem_total_gb": 32.0, "swap_used_pct": None, "cap": 4, "refusal": "", "lanes_placed": 1, "lanes": ["lane-x"],
             "units_placed": 0, "unit_work": 0, "runners": {"state": "unread"}},
            {"host": "m2", "label": "Bay two", "state": "repair", "note": "down for repair since the move", "read_at": "",
             "read_age_min": None, "stale": False, "cap": 2, "refusal": "", "lanes_placed": 0, "lanes": [], "runners": {"state": "unread"}},
        ],
        "cloud": {"state": "read", "note": "", "accounts": [], "sessions": [], "lamps": []},
        "lanes": [
            {"lane": "lane-x", "ticket": "OMN-1", "title": "Ticket one", "title_source": "claim", "kind": "fix", "engine": "claude",
             "placement": "machine", "host": "m1", "cloud_account": "", "started_at": "2099-01-01T00:00:00Z", "run_minutes": 5,
             "status_text": "Rebuilding the widget", "status_full": "Rebuilding the widget in full", "status_at": "2099-01-01T00:00:00Z",
             "status_age_min": 1, "status_source": "status", "silent_minutes": 0, "stalled": False, "pr": "repo#9",
             "pr_title": "widget", "pr_state": "OPEN", "pr_ci": "red", "pr_ci_note": "2 red of 10", "station": None, "claim_full": "c"},
            {"lane": "lane-y", "ticket": "", "title": "", "title_source": "unread", "kind": "lane", "engine": "claude",
             "placement": "unplaced", "host": "", "cloud_account": "", "started_at": "2099-01-01T00:00:00Z", "run_minutes": 1,
             "status_text": "", "status_full": "", "status_at": "2099-01-01T00:00:00Z", "status_age_min": 1, "status_source": "claim",
             "silent_minutes": 60, "stalled": True, "pr": "", "pr_title": "", "pr_state": "", "pr_ci": "", "pr_ci_note": "",
             "station": None, "claim_full": ""},
        ],
        "stall_minutes": 45,
        "lane_window_minutes": 180,
        "bays": [],
        "shipped": [{"pr": "repo#7", "title": "Shipped seven", "line_id": "plan-two", "detail": "merged 2099-01-01T00:05:00Z"}],
        "waiting": [],
        "stoppages": [],
    },
    "sources": {"watcher_last_tick": "2000-01-01T00:00:00Z", "ledger_rows": 7311, "ledger_newest_row": "2099-01-01T00:00:00Z",
                "linear_issues": 12, "placement_hosts": 2, "stoppage_report": False, "pool_read_at": "2099-01-01T00:00:00Z",
                "host_table_read": True, "placement_markers_read": False, "receipts_read": True, "cloud_accounts_read": True,
                "runner_fleet_read": False},
}  # fmt: skip

SHELL_STATUS = {
    "published_at": "2099-01-01T00:01:00Z",
    "steps": [],
    "lines_declared": {
        "state": "read",
        "plan_path_contains": ["two-plan", "never-matched-plan"],
    },
}

SHELL_HEALTH = {
    "generated_at": "2099-01-01T00:00:00Z",
    "summary": {"measured": True, "improved": 3, "worse": 1, "flat": 2, "unmeasured": 0},
    "sources": [{"name": "ledger", "status": "read"}, {"name": "landing", "status": "partial"}],
    "days": [
        {"date": "2098-12-31", "partial": False, "window": {"end": "2099-01-01T00:00:00Z", "metrics": [
            {"key": "merges.total", "value": 161.0, "status": "read"}]}},
        {"date": "2099-01-01", "partial": True, "window": {"end": "2099-01-01T00:00:00Z", "metrics": [
            {"key": "merges.total", "value": 232.0, "status": "partial"},
            {"key": "merges.product", "value": 178.0, "status": "partial"},
            {"key": "reds.episodes_opened", "value": 9.0, "status": "partial"},
            {"key": "reds.hours_red", "value": 45.0, "status": "partial"}]}},
    ],
}  # fmt: skip

SHELL_MANIFEST = {
    "schema": "omni.reports-site/1",
    "published_at": "2099-01-01T00:01:00Z",
    "reports": [
        {"id": "factory_floor", "title": "Floor", "refresh": "tick", "data": [
            {"name": "Factory record", "status": "read", "as_of": "2099-01-01T00:00:00Z"}]},
        {"id": "process_health", "title": "Process", "refresh": "tick", "data": [
            {"name": "Process-health series", "status": "read", "as_of": "2099-01-01T00:00:00Z"}]},
        {"id": "decision_map", "title": "Map", "refresh": "snapshot", "data": [
            {"name": "Decision map data", "status": "read", "as_of": "2098-12-30T00:00:00Z"}]},
        {"id": "decision_quality", "title": "Quality", "refresh": "snapshot", "data": [
            {"name": "Decision quality report", "status": "unread", "as_of": None, "reason": "not readable (FileNotFoundError)"}]},
    ],
}  # fmt: skip

SHELL_FILES = {
    "factory-floor.json": SHELL_RECORD,
    "publish-status.json": SHELL_STATUS,
    "process-health.json": SHELL_HEALTH,
    "reports/manifest.json": SHELL_MANIFEST,
}


def _run_shell(files: dict[str, object], **opts: object) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    done = subprocess.run(
        [
            node,
            "-e",
            SHELL_HARNESS,
            str(NODE_DIR / "page/index.html"),
            json.dumps({k: json.dumps(v) for k, v in files.items()}),
            json.dumps(opts),
        ],
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr.decode()
    return cast(
        "dict[str, object]", json.loads(done.stdout.decode().strip().splitlines()[-1])
    )


def _frames(result: dict[str, object]) -> dict[str, list[str]]:
    return cast("dict[str, list[str]]", result["frames"])


def _shell_out(result: dict[str, object]) -> dict[str, str]:
    return cast("dict[str, str]", result["out"])


def test_page_tabs_the_site_is_one_page_with_five_tabs_and_the_dashboard_first() -> (
    None
):
    labels = re.findall(r'\{id: "(\w+)", label: "([^"]+)"', SHELL)
    assert labels == [
        ("dashboard", "Dashboard"),
        ("floor", "Factory floor"),
        ("process", "Process before and after"),
        ("map", "Decision map"),
        ("quality", "Decision quality"),
    ]
    for pane in ("dashboard", "floor", "process", "map", "quality"):
        assert f'id="pane-{pane}"' in SHELL
    assert 'role="tablist"' in SHELL
    for page in (
        'page: "floor/?embed=1"',
        'page: "reports/process-health/?embed=1"',
        'page: "reports/decision-map/?embed=1"',
        'page: "reports/decision-quality/?embed=1"',
    ):
        assert page in SHELL


def test_page_tabs_open_the_dashboard_with_no_hash_and_no_remembered_tab() -> None:
    result = _run_shell(SHELL_FILES)
    assert result["sel"] == ["tab-dashboard"]
    assert result["shown"] == ["pane-dashboard"]
    assert result["hash"] == "#dashboard"
    assert _frames(result)["pane-floor"] == []


def test_page_tabs_the_hash_picks_the_tab_and_the_tab_page_draws_inside_the_page() -> (
    None
):
    result = _run_shell(SHELL_FILES, hash="#process")
    assert result["sel"] == ["tab-process"]
    assert result["shown"] == ["pane-process"]
    assert _frames(result)["pane-process"] == ["reports/process-health/?embed=1"]
    assert result["saved"] == {"factory-floor.tab": "process"}
    assert "Process before and after" in str(result["title"])
    other = _run_shell(SHELL_FILES, hash="#floor/bay-m1")
    assert other["sel"] == ["tab-floor"]
    assert _frames(other)["pane-floor"] == ["floor/?embed=1"]


def test_page_tabs_a_hash_that_names_no_tab_falls_back_to_the_remembered_one() -> None:
    result = _run_shell(SHELL_FILES, hash="#nonsense", stored="map")
    assert result["sel"] == ["tab-map"]
    result = _run_shell(SHELL_FILES, stored="quality")
    assert result["sel"] == ["tab-quality"]
    assert result["hash"] == "#quality"


def test_page_tabs_blocked_browser_storage_still_opens_the_dashboard() -> None:
    result = _run_shell(SHELL_FILES, storeBlocked=True)
    assert result["sel"] == ["tab-dashboard"]
    assert "Second plan" in _shell_out(result)["dash"]
    assert "try {" in SHELL


def test_page_dashboard_draws_each_line_with_its_state_and_its_real_stop_reason() -> (
    None
):
    out = _shell_out(_run_shell(SHELL_FILES))
    dash = out["dash"]
    assert "Second plan" in dash
    assert "Stopped" in dash
    assert "PR repo#9 CI red (2 red of 10)" in dash
    assert "37% built" in dash
    assert "never-matched-plan" in dash
    assert "not in the record" in dash
    assert dash.index("Line A") < dash.index("Line B")
    assert "#dashboard/line-plan-two" in dash


def test_page_dashboard_draws_each_machine_with_its_lanes_load_and_memory() -> None:
    dash = _shell_out(_run_shell(SHELL_FILES))["dash"]
    assert "Bay one" in dash
    assert "Rebuilding the widget" in dash
    assert '1</span> lanes placed of <span class="mono">4' in dash
    assert "load per core" in dash
    assert "12.5 GB of 32" in dash
    assert "repo#9 · CI red" in dash
    assert "Bay two" in dash
    assert "in repair" in dash
    assert "down for repair since the move" in dash
    assert "1 live lane names no machine the floor draws" in dash
    assert "stalled" in dash
    assert "#dashboard/lane-lane-x" in dash


def test_page_dashboard_takes_todays_merges_and_reds_from_the_series_and_the_lanes() -> (
    None
):
    dash = _shell_out(_run_shell(SHELL_FILES))["dash"]
    assert ">232<" in dash
    assert "178 to product code" in dash
    assert "the day before 161" in dash
    assert "9 dev-branch red episodes opened today" in dash
    assert "45 repo-hours red" in dash
    assert "Shipped seven" in dash
    assert "UTC day 2099-01-01, so far" in dash
    for baked in ("232 merges", "251 of 251"):
        assert baked not in SHELL


def test_page_dashboard_says_the_series_is_unread_instead_of_drawing_a_zero() -> None:
    files = {k: v for k, v in SHELL_FILES.items() if k != "process-health.json"}
    out = _shell_out(_run_shell(files))
    assert "The process-health series is not published" in out["dash"]
    assert ">232<" not in out["dash"]
    assert "series not published" in out["sub-process"]


def test_page_dashboard_shows_the_age_of_every_source_and_marks_an_old_or_unread_one() -> (
    None
):
    out = _shell_out(_run_shell(SHELL_FILES))
    dash = out["dash"]
    assert "Pull request watcher, last tick" in dash
    assert (
        'class="age-stale"' in dash
    )  # the watcher tick is decades older than the record
    assert "Placement markers (units)" in dash
    assert "CI runner fleet observation" in dash
    assert "Decision quality report" in dash
    assert "not readable (FileNotFoundError)" in dash
    assert "a snapshot, not refreshed by a tick" in dash
    assert "Process-health series" in dash
    assert "1 of 2 inputs read" in dash
    assert dash.count("Process-health series") == 1


def test_page_tabs_carry_a_lamp_and_a_caption_read_from_the_same_files() -> None:
    out = _shell_out(_run_shell(SHELL_FILES))
    assert "1 of 2 lines stopped" in out["sub-dashboard"]
    assert "3 better, 1 worse" in out["sub-process"]
    assert "snapshot" in out["sub-map"]
    assert "data unread" in out["sub-quality"]


def test_page_dashboard_without_a_record_draws_nothing_and_says_it_is_unread() -> None:
    out = _shell_out(_run_shell({}))
    assert "the factory record is unread" in out["dash"]
    assert "could not be read" in out["banner"]
    assert "unread" in out["sub-dashboard"]


def test_page_tabs_shell_carries_no_secret_no_host_and_no_external_url() -> None:
    assert not re.search(r"https?://|//[A-Za-z0-9.-]+\.[a-z]{2,}", SHELL)
    assert not re.search(r"/(?:Users|home|Volumes)/", SHELL)
    assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", SHELL)
    assert not re.search(r"\bh0\d\d\b|\.ts\.net|tailscale|ssh", SHELL, re.IGNORECASE)
    assert not re.search(
        r"secret|password|api[_-]?key|bearer|token", SHELL, re.IGNORECASE
    )
    assert SHELL.count("<link") == 1
    assert "src=" not in SHELL
    assert SHELL.count("${esc(") > 20
    for raw in ("${L.name}", "${m.label}", "${l.title}", "${p.title}"):
        assert raw not in SHELL


def test_page_tabs_shell_node_syntax_is_valid() -> None:
    script = re.search(r"<script>(.*)</script>", SHELL, re.DOTALL | re.IGNORECASE)
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


def test_page_drawer_a_hash_naming_a_lane_opens_the_panel_over_the_dashboard() -> None:
    result = _run_shell(SHELL_FILES, hash="#dashboard/lane-lane-x")
    out = _shell_out(result)
    assert result["sel"] == ["tab-dashboard"]
    assert "Ticket one" in out["drawerTitle"]
    assert "Rebuilding the widget in full" in out["drawerBody"]
    assert "OMN-1" in out["drawerBody"]
    assert result["hash"] == "#dashboard/lane-lane-x"


def test_page_drawer_a_line_a_machine_and_a_station_open_from_their_hash() -> None:
    line = _shell_out(_run_shell(SHELL_FILES, hash="#dashboard/line-plan-two"))
    assert "Second plan" in line["drawerTitle"]
    assert "Look first" in line["drawerBody"]
    assert "Part B" in line["drawerBody"]
    bay = _shell_out(_run_shell(SHELL_FILES, hash="#floor/bay-m1"))
    assert "Bay one" in bay["drawerTitle"]
    assert "Rebuilding the widget" in bay["drawerBody"]
    station = _shell_out(_run_shell(SHELL_FILES, hash="#dashboard/st-plan-two-0"))
    assert "Look first" in station["drawerTitle"]
    assert "Part A" in station["drawerBody"]


def test_page_drawer_a_name_the_record_does_not_hold_says_so_instead_of_drawing_nothing() -> (
    None
):
    out = _shell_out(_run_shell(SHELL_FILES, hash="#dashboard/lane-gone"))
    assert "Not in the record" in out["drawerTitle"]
    assert "gone" in out["drawerBody"]


def test_page_drawer_is_a_labelled_dialog_closed_by_button_escape_scrim_and_back() -> (
    None
):
    assert 'role="dialog"' in SHELL
    assert 'aria-labelledby="drawerTitle"' in SHELL
    assert 'id="drawerClose"' in SHELL
    assert 'ev.key === "Escape"' in SHELL
    assert 'el("scrim").addEventListener("click", closeByUser)' in SHELL
    assert 'addEventListener("popstate", sync)' in SHELL
    assert "history.pushState" in SHELL
    assert "history.replaceState" in SHELL


def test_page_drawer_never_scrolls_the_tab_under_it() -> None:
    assert "scrollIntoView" not in SHELL
    assert "preventScroll: true" in SHELL
    assert "saveScroll" in SHELL
    assert "restoreScroll" in SHELL
    # the in-page anchor jumps are gone from the dashboard's links
    assert 'href="#floor/' not in SHELL
    # inside the board the floor page asks the board's panel to open and scrolls nothing
    embedded = PAGE.split("if (EMBEDDED){", 1)[1].split("const a = t.closest", 1)[0]
    assert "parent.postMessage" in PAGE
    assert "scrollIntoView" not in embedded


def test_page_drawer_motion_is_short_and_off_under_reduced_motion() -> None:
    assert "transition:transform .18s ease,opacity .18s ease" in SHELL
    reduced = SHELL.split("@media (prefers-reduced-motion:reduce){", 1)[1]
    assert ".drawer,.drawer.open,.scrim,.scrim.open{transition:none}" in reduced
    assert ".pane:not([hidden]){animation:none}" in reduced


def test_page_drawer_the_phone_sheet_is_full_width_with_a_scrim_and_the_desktop_panel_is_not_dimmed() -> (
    None
):
    assert "width:40vw" in SHELL
    phone = SHELL.split("@media (max-width:700px){\n  .drawer{", 1)[1]
    assert "width:100%" in phone.split("}", 1)[0]
    assert ".scrim{display:none}" in SHELL
    assert 'el("app").inert = phone()' in SHELL
