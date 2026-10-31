# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor publish node: overlay-only host values, atomic remote writes, and the page it ships."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from collections.abc import Sequence
from pathlib import Path

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


def _program_calls(runner: LocalSsh) -> list[str]:
    return [
        c[0][2] if c[0][1] == "-m" else "hosts"
        for c in runner.calls
        if c[0][0] != "ssh"
    ]


def test_refresh_steps_run_in_contract_order_with_the_factory_floor_effect_last(
    tmp_path: Path,
) -> None:
    runner = Scripted({"--json": (0, b"[]", b"")})
    request = _refresh_request(tmp_path)
    HandlerFactoryFloorPublish(runner).handle(request, environ={})
    assert _program_calls(runner) == ["hosts", HEALTH_MODULE, FACTORY]
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
        _refresh_request(tmp_path), environ={}
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
        "andon", "flag", "reason", "causes", "items", "feeders", "bays", "shipped", "waiting", "stoppages",
        "cost_minutes", "occurrences", "recurring", "tickets", "last_seen", "stale", "running", "free",
        "watcher_last_tick", "ledger_rows", "ledger_newest_row", "linear_issues", "placement_hosts",
        "stoppage_report", "comparisons", "before_status", "after_status", "verdict", "is_reset_day",
        "covered_to", "reset_label", "publish-status.json", "process-health.json", "published_at",
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
    script = re.search(r"<script>(.*)</script>", PAGE, re.DOTALL)
    assert script is not None
    node = subprocess.run(["node", "--version"], capture_output=True, check=False)
    if node.returncode != 0:
        pytest.skip("node is not installed")
    checked = subprocess.run(
        ["node", "--check", "-"],
        input=script.group(1).encode(),
        capture_output=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr.decode()
