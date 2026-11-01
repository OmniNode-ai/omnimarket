# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reports site: its contract, the files it publishes and the age it states, and the pages it ships."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    NODE_DIR,
    PublishRefusedError,
    load_runtime,
    remote_write_command,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_reports_publish import (
    MANIFEST_SCHEMA,
    HandlerReportsPublish,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishOverlay,
    ModelReportData,
    ModelReportPage,
    ModelReportsPublishRequest,
)
from omnimarket.nodes.node_factory_floor_publish_effect.runtime_factory_floor_publish import (
    main,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 31, 16, 0, 0, tzinfo=UTC)
FLOOR_RECORD = {"generated_at": "2026-10-31T15:40:00+00:00", "lines": []}
HEALTH = {"generated_at": "2026-10-31T15:30:00Z", "days": [], "summary": {}}
DECISIONS = {"asof": "2026-10-30", "window": "2026-09-30 to 2026-10-30", "records": []}
REPORT_TEXT = "# Decision quality\n\nA paragraph.\n"
REPORT_MTIME = datetime(2026, 10, 31, 8, 11, 0, tzinfo=UTC)


class LocalSsh:
    """Stands in for ssh: runs the remote command with a local shell, so the atomic write really executes."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bytes | None]] = []

    def __call__(
        self, argv: Sequence[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((list(argv), stdin))
        return subprocess.run(
            ["sh", "-c", argv[-1]], input=stdin, capture_output=True, check=False
        )


def _put(home: Path, relpath: str, data: bytes, mtime: datetime | None = None) -> None:
    path = home / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime.timestamp(), mtime.timestamp()))


def _sources(home: Path) -> dict[str, str]:
    """Write every data file the contract's reports read, and return the relpath of each by data remote name."""
    paths = {
        d.remote: d.relpath for e in load_runtime().reports.entries for d in e.data
    }
    _put(home, paths["factory-floor.json"], json.dumps(FLOOR_RECORD).encode())
    _put(home, paths["process-health.json"], json.dumps(HEALTH).encode())
    _put(
        home,
        paths["reports/decision-map/decisions.json"],
        json.dumps(DECISIONS).encode(),
    )
    _put(
        home,
        paths["reports/decision-quality/decision-quality.md"],
        REPORT_TEXT.encode(),
        REPORT_MTIME,
    )
    return paths


def _request(tmp_path: Path) -> ModelReportsPublishRequest:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    _sources(home)
    return ModelReportsPublishRequest(
        home=str(home),
        overlay=ModelFactoryFloorPublishOverlay(
            ssh_target="lab-web",
            remote_dir=str(tmp_path / "site"),
            serve_url="http://example.invalid:8088/",
            bus_lane="lane-x",
        ),
        now=NOW,
    )


def _manifest(tmp_path: Path) -> dict[str, Any]:
    document = json.loads((tmp_path / "site/reports/manifest.json").read_text())
    return {r["id"]: r for r in document["reports"]}


def test_reports_contract_ties_the_floor_entries_to_the_floor_publish_paths() -> None:
    rt = load_runtime()
    by_remote = {d.remote: d for e in rt.reports.entries for d in e.data}
    floor = by_remote[rt.names.data]
    health = by_remote[rt.names.process_health]
    assert floor.relpath == rt.source.relpath
    assert health.relpath == rt.process_health_relpath
    assert floor.publish is False
    assert health.publish is False
    assert floor.as_of_key == health.as_of_key == "generated_at"


def test_reports_contract_every_page_exists_and_every_link_lands_on_a_published_page() -> (
    None
):
    rt = load_runtime()
    published = {rt.names.page, rt.names.floor_page, rt.reports.index.remote}
    for entry in rt.reports.entries:
        for page in entry.pages:
            assert (NODE_DIR / page.page_relpath).is_file(), page.page_relpath
            published.add(page.remote)
    assert (NODE_DIR / rt.reports.index.page_relpath).is_file()
    base = PurePosixPath(rt.reports.index.remote).parent
    for entry in rt.reports.entries:
        resolved = PurePosixPath(os.path.normpath(base / entry.href))
        target = (resolved / "index.html").as_posix().removeprefix("./")
        assert target in published, (entry.id, target)


def test_reports_contract_every_published_name_is_written_by_one_report() -> None:
    rt = load_runtime()
    names = [rt.reports.index.remote, rt.reports.manifest]
    for entry in rt.reports.entries:
        names += [p.remote for p in entry.pages]
        names += [d.remote for d in entry.data if d.publish]
    assert len(names) == len(set(names))


@pytest.mark.parametrize(
    "name", ["../x", "/abs/x", ".hidden/x", "a//b", "a/.b", "", "a\\b"]
)
def test_reports_published_names_must_be_plain_relative_paths(name: str) -> None:
    with pytest.raises(ValidationError):
        ModelReportPage(page_relpath="page/x.html", remote=name)
    with pytest.raises(ValidationError):
        ModelReportData(name="x", remote=name, relpath="a/b.json")


def test_reports_remote_write_keeps_the_temporary_beside_the_final_file() -> None:
    cmd = remote_write_command("/srv/site", "reports/decision-map/decisions.json")
    assert "mkdir -p /srv/site/reports/decision-map" in cmd
    assert (
        "mv -f /srv/site/reports/decision-map/.decisions.json.tmp "
        "/srv/site/reports/decision-map/decisions.json"
    ) in cmd


def test_reports_publish_writes_the_index_pages_data_and_manifest(
    tmp_path: Path,
) -> None:
    ssh = LocalSsh()
    result = HandlerReportsPublish(ssh).handle(_request(tmp_path), environ={})
    site = tmp_path / "site"
    assert set(result.published) == {
        "reports/index.html",
        "reports/process-health/index.html",
        "reports/decision-map/index.html",
        "reports/decision-map/decisions.json",
        "reports/decision-quality/index.html",
        "reports/decision-quality/decision-quality.md",
        "reports/manifest.json",
    }
    assert result.unread == ()
    assert result.published[-1] == "reports/manifest.json"
    assert (site / "reports/decision-map/decisions.json").read_bytes() == json.dumps(
        DECISIONS
    ).encode()
    assert (site / "reports/index.html").read_bytes() == (
        NODE_DIR / "page/reports/index.html"
    ).read_bytes()
    assert [p for p in site.rglob("*") if p.name.startswith(".")] == []
    assert not (site / "factory-floor.json").exists()
    assert not (site / "process-health.json").exists()
    assert result.index_url == "http://example.invalid:8088/reports/index.html"
    assert result.manifest_sha256


def test_reports_manifest_states_each_data_age_and_what_it_is_measured_from(
    tmp_path: Path,
) -> None:
    HandlerReportsPublish(LocalSsh()).handle(_request(tmp_path), environ={})
    document = json.loads((tmp_path / "site/reports/manifest.json").read_text())
    assert document["schema"] == MANIFEST_SCHEMA
    assert document["published_at"] == "2026-10-31T16:00:00Z"
    reports = _manifest(tmp_path)
    assert list(reports) == [
        "factory_floor",
        "process_health",
        "decision_map",
        "decision_quality",
    ]
    floor = reports["factory_floor"]["data"][0]
    assert floor["as_of"] == "2026-10-31T15:40:00Z"
    assert floor["status"] == "read"
    assert "generated_at" in floor["as_of_basis"]
    assert reports["decision_map"]["refresh"] == "snapshot"
    assert reports["decision_map"]["data"][0]["as_of"] == "2026-10-30T00:00:00Z"
    quality = reports["decision_quality"]["data"][0]
    assert quality["as_of"] == "2026-10-31T08:11:00Z"
    assert quality["as_of_basis"] == "the file's modified time"
    assert reports["process_health"]["refresh"] == "tick"


def test_reports_an_unreadable_source_is_listed_unread_and_not_published(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    paths = {
        d.remote: d.relpath for e in load_runtime().reports.entries for d in e.data
    }
    (Path(request.home or "") / paths["reports/decision-map/decisions.json"]).unlink()
    (Path(request.home or "") / paths["process-health.json"]).write_text(
        "{not json", encoding="utf-8"
    )
    (Path(request.home or "") / paths["factory-floor.json"]).write_text(
        '{"lines": []}', encoding="utf-8"
    )
    result = HandlerReportsPublish(LocalSsh()).handle(request, environ={})
    assert result.unread == (
        "factory_floor:factory-floor.json",
        "process_health:process-health.json",
        "decision_map:reports/decision-map/decisions.json",
    )
    assert "reports/decision-map/decisions.json" not in result.published
    assert "reports/decision-map/index.html" in result.published
    reports = _manifest(tmp_path)
    for rid, reason in (
        ("decision_map", "not readable"),
        ("process_health", "not JSON"),
        ("factory_floor", "no readable time at key generated_at"),
    ):
        row = reports[rid]["data"][0]
        assert row["status"] == "unread"
        assert row["as_of"] is None
        assert row["reason"].startswith(reason)


def test_reports_a_failed_copy_leaves_the_previous_file_and_names_it(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site/reports"
    site.mkdir(parents=True)
    (site / "manifest.json").write_text("previous", encoding="utf-8")

    def failing(
        argv: Sequence[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        if "manifest.json" in argv[-1]:
            return subprocess.CompletedProcess(list(argv), 1, b"", b"disk full")
        return LocalSsh()(argv, stdin, timeout)

    with pytest.raises(PublishRefusedError, match=r"manifest\.json failed"):
        HandlerReportsPublish(failing).handle(_request(tmp_path), environ={})
    assert (site / "manifest.json").read_text() == "previous"


def test_reports_cli_publishes_the_floor_and_then_the_reports_unless_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    request = _request(tmp_path)
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        f"ssh_target: lab-web\nremote_dir: {tmp_path / 'site'}\n"
        "serve_url: http://example.invalid:8088/\nbus_lane: lane-x\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_FACTORY_FLOOR_PUBLISH_OVERLAY", str(overlay))
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ssh = fake_bin / "ssh"
    ssh.write_text('#!/bin/sh\nfor last; do :; done\nexec sh -c "$last"\n')
    ssh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    assert main(["--omni-home", request.home or "", "--skip-refresh"]) == 0
    out = capsys.readouterr().out
    assert "factory-floor-publish: published=" in out
    assert "reports-site-publish: published=7 files unread=none" in out
    assert (tmp_path / "site/reports/index.html").is_file()
    shutil.rmtree(tmp_path / "site")
    assert (
        main(["--omni-home", request.home or "", "--skip-refresh", "--skip-reports"])
        == 0
    )
    assert "reports-site-publish" not in capsys.readouterr().out
    assert not (tmp_path / "site/reports").exists()


# ---- the pages -----------------------------------------------------------------------------------------------

REPORT_PAGES = {
    "index": NODE_DIR / "page/reports/index.html",
    "process-health": NODE_DIR / "page/reports/process-health/index.html",
    "decision-map": NODE_DIR / "page/reports/decision-map/index.html",
    "decision-quality": NODE_DIR / "page/reports/decision-quality/index.html",
}
PINNED_SCRIPTS = {
    "https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.28.1/cytoscape.min.js",
    "https://cdn.jsdelivr.net/npm/elkjs@0.9.2/lib/elk.bundled.js",
}


@pytest.mark.parametrize("name", sorted(REPORT_PAGES))
def test_page_reports_carry_no_secret_no_absolute_path_no_host_and_no_stray_url(
    name: str,
) -> None:
    page = REPORT_PAGES[name].read_text(encoding="utf-8")
    urls = set(re.findall(r"https?://[^\s\"'<>)]+", page))
    assert urls == (PINNED_SCRIPTS if name == "decision-map" else set()), urls
    assert not re.search(r"/(?:Users|home|Volumes)/", page)
    assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", page)
    assert not re.search(r"\bh0\d\d\b|\.ts\.net|tailscale|ssh", page, re.IGNORECASE)
    assert not re.search(
        r"secret|password|api[_-]?key|bearer|token", page, re.IGNORECASE
    )
    assert "@import" not in page
    assert "fonts.googleapis" not in page


def test_page_reports_decision_map_scripts_are_pinned_with_integrity_hashes() -> None:
    page = REPORT_PAGES["decision-map"].read_text(encoding="utf-8")
    tags = re.findall(r"<script\b[^>]*\bsrc=[^>]*>", page)
    assert {m for t in tags for m in re.findall(r'src="([^"]+)"', t)} == PINNED_SCRIPTS
    for tag in tags:
        assert re.search(r'integrity="sha384-[A-Za-z0-9+/]{64}"', tag), tag
        assert 'crossorigin="anonymous"' in tag
    assert 'fetch("decisions.json")' in page


@pytest.mark.parametrize("name", ["index", "process-health", "decision-quality"])
def test_page_reports_load_no_script_or_style_from_elsewhere(name: str) -> None:
    page = REPORT_PAGES[name].read_text(encoding="utf-8")
    assert "src=" not in page
    assert page.count("<link") == 1
    assert '<link rel="icon" href="data:,">' in page


def test_page_reports_every_report_page_links_back_and_the_tab_page_draws_each_in_place() -> (
    None
):
    for name in ("process-health", "decision-map", "decision-quality"):
        assert 'href="../"' in REPORT_PAGES[name].read_text(encoding="utf-8"), name
    shell = (NODE_DIR / "page/index.html").read_text(encoding="utf-8")
    for name in ("process-health", "decision-map", "decision-quality"):
        assert f'page: "reports/{name}/?embed=1"' in shell, name
    assert 'href="reports/"' not in shell


def _inline_scripts(page: str) -> list[str]:
    return re.findall(r"<script>(.*?)</script>", page, re.DOTALL | re.IGNORECASE)


@pytest.mark.parametrize("name", sorted(REPORT_PAGES))
def test_page_reports_node_syntax_is_valid(name: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    scripts = _inline_scripts(REPORT_PAGES[name].read_text(encoding="utf-8"))
    assert scripts
    for script in scripts:
        checked = subprocess.run(
            [node, "--check", "-"],
            input=script.encode(),
            capture_output=True,
            check=False,
        )
        assert checked.returncode == 0, checked.stderr.decode()


HARNESS = textwrap.dedent(
    """
    const fs = require("fs");
    const [pagePath, filesJson] = process.argv.slice(1);
    const files = JSON.parse(filesJson);
    const page = fs.readFileSync(pagePath, "utf8");
    const script = /<script>([\\s\\S]*?)<\\/script>/.exec(page)[1];
    const elements = {};
    const element = id => elements[id] || (elements[id] = {id, innerHTML: "", textContent: "", hidden: true});
    globalThis.document = {getElementById: element};
    globalThis.setInterval = () => 0;
    globalThis.fetch = async url => {
      const key = Object.keys(files).find(k => url.endsWith(k));
      if (!key) return {ok: false, status: 404, json: async () => ({}), text: async () => ""};
      return {ok: true, status: 200, json: async () => JSON.parse(files[key]), text: async () => files[key]};
    };
    (async () => {
      new Function(script).call(globalThis);
      await new Promise(r => setTimeout(r, 100));
      const out = {};
      for (const [k, v] of Object.entries(elements)) out[k] = [v.innerHTML, v.textContent, String(v.hidden)].join("|");
      console.log(JSON.stringify(out));
    })();
    """
)


def _run_page(page: Path, files: dict[str, str]) -> dict[str, str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    done = subprocess.run(
        [node, "-e", HARNESS, str(page), json.dumps(files)],
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr.decode()
    return cast(
        "dict[str, str]", json.loads(done.stdout.decode().strip().splitlines()[-1])
    )


def test_page_reports_index_shows_age_unread_and_snapshot_for_each_card() -> None:
    manifest = {
        "published_at": "2026-10-31T16:00:00Z",
        "reports": [
            {
                "id": "a",
                "title": "Live <b>report</b>",
                "summary": "s",
                "href": "a/",
                "refresh": "tick",
                "refresh_note": "",
                "data": [
                    {
                        "name": "record",
                        "remote": "a.json",
                        "status": "read",
                        "as_of": "2020-01-01T00:00:00Z",
                        "as_of_basis": "the file's own generated_at value",
                        "reason": "",
                    }
                ],
            },
            {
                "id": "b",
                "title": "Snapshot",
                "summary": "s",
                "href": "b/",
                "refresh": "snapshot",
                "refresh_note": "No node writes this.",
                "data": [
                    {
                        "name": "missing",
                        "remote": "b.json",
                        "status": "unread",
                        "as_of": None,
                        "as_of_basis": None,
                        "reason": "not readable (FileNotFoundError)",
                    }
                ],
            },
        ],
    }
    out = _run_page(REPORT_PAGES["index"], {"manifest.json": json.dumps(manifest)})
    grid = out["grid"]
    assert "Live &lt;b&gt;report&lt;/b&gt;" in grid
    assert "<b>report</b>" not in grid
    assert "days old" in grid
    assert 'class="ag stale"' in grid
    assert "unread" in grid
    assert "not readable (FileNotFoundError)" in grid
    assert "No node writes this." in grid
    assert "Snapshot</span>" in grid


def test_page_reports_index_without_a_manifest_says_so() -> None:
    out = _run_page(REPORT_PAGES["index"], {})
    assert "could not be read (HTTP 404)" in out["banner"]


QUALITY_MD = "\n".join(
    [
        "# Title <script>alert(1)</script>",
        "",
        "Para with `code <b>` and **bold** and *italic*.",
        "",
        "## Section one",
        "",
        "1. First **bold**",
        "2. Second",
        "",
        "- Item",
        "  - Nested *deep*",
        "",
        "| A | B |",
        "|---|---|",
        "| 1 | <img src=x onerror=alert(1)> |",
        "",
        "## Section two",
        "tail",
    ]
)


def test_page_reports_decision_quality_renders_markdown_and_escapes_everything() -> (
    None
):
    manifest = {
        "reports": [
            {
                "id": "decision_quality",
                "data": [
                    {
                        "status": "read",
                        "as_of": "2026-10-31T08:11:00Z",
                    }
                ],
            }
        ]
    }
    out = _run_page(
        REPORT_PAGES["decision-quality"],
        {"decision-quality.md": QUALITY_MD, "manifest.json": json.dumps(manifest)},
    )
    doc = out["doc"]
    assert "<script>" not in doc
    assert "<img" not in doc
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in doc
    # The opening paragraph says how the report was made; it moves to the footer.
    how = out["how"]
    assert "<code>code &lt;b&gt;</code>" in how
    assert "<p>Para with" not in doc
    assert "<strong>bold</strong>" in doc
    assert "<em>italic</em>" in how
    assert "<ol><li>First <strong>bold</strong></li><li>Second</li></ol>" in doc
    assert "<ul><li>Item<ul><li>Nested <em>deep</em></li></ul></li></ul>" in doc
    assert "<th>A</th><th>B</th>" in doc
    assert "&lt;img src=x onerror=alert(1)&gt;" in doc
    assert 'href="#' in out["toc"]
    assert "Section two" in out["toc"]
    assert "Report file modified" in out["stamp"]


def test_page_reports_process_health_shows_unread_never_zero() -> None:
    comparison = {
        "key": "regressions.tickets_reopened",
        "label": "Closed tickets reopened",
        "unit": "reopens",
        "kind": "count",
        "polarity": "lower_is_better",
        "before": None,
        "after": None,
        "delta": None,
        "delta_ratio": None,
        "verdict": "unmeasured",
        "before_status": "unread",
        "after_status": "unread",
        "comparable_unit": "reopens per day",
        "note": "no state history",
    }
    quality = {
        **comparison,
        "key": "reds.hours_red",
        "label": "Hours red",
        "before": 4.0,
        "after": 6.0,
        "delta": 2.0,
        "delta_ratio": 0.5,
        "verdict": "worse",
        "before_status": "read",
        "after_status": "partial",
    }
    grey = {
        **quality,
        "key": "merges.total",
        "label": "Merges",
        "verdict": "informational",
    }

    def metric(key: str, value: float | None, status: str) -> dict[str, object]:
        return {
            "key": key,
            "label": key,
            "unit": "u",
            "kind": "count",
            "value": value,
            "status": status,
        }

    days = [
        {
            "date": f"2026-10-{d:02d}",
            "is_reset_day": d == 29,
            "partial": d == 30,
            "window": {
                "metrics": [
                    metric("reds.hours_red", float(d - 25), "read"),
                    metric("regressions.tickets_reopened", None, "unread"),
                    metric("merges.total", 3.0, "read"),
                ]
            },
        }
        for d in (28, 29, 30)
    ]
    series = {
        "generated_at": "2026-10-31T15:30:00Z",
        "reset_label": "Factory reset",
        "sources": [
            {
                "name": "ledger",
                "status": "partial",
                "covered_from": "2026-10-16T14:57:26Z",
                "covered_to": "2026-10-31T14:57:19Z",
                "gap_hours": 0.0,
                "records": 12,
                "reason": "25 files <b>parsed</b>",
            }
        ],
        "days": days,
        "summary": {
            "measured": True,
            "window_hours": 61.5,
            "before": {"start": "2026-10-26T12:00:00Z", "end": "2026-10-29T01:30:00Z"},
            "after": {"start": "2026-10-29T01:30:00Z", "end": "2026-10-31T15:00:00Z"},
            "improved": 0,
            "worse": 1,
            "flat": 0,
            "unmeasured": 1,
            "comparisons": [quality, comparison, grey],
        },
    }
    out = _run_page(
        REPORT_PAGES["process-health"], {"process-health.json": json.dumps(series)}
    )
    body = out["body"]
    assert 'class="unread">unread</span>' in body
    assert "unread on every day" in body
    assert "25 files &lt;b&gt;parsed&lt;/b&gt;" in body
    assert "(partial)" in body
    assert body.index("Quality, before and after") < body.index("Throughput and cost")
    grey_block = body[body.index('<section class="grey">') :]
    assert "Merges" in grey_block
    assert "Closed tickets reopened" not in grey_block
    assert "Process health · computed" in out["stamp"]


def test_page_reports_process_health_without_a_series_shows_unread_not_zero() -> None:
    out = _run_page(REPORT_PAGES["process-health"], {})
    assert "unread (the series is not published)" in out["banner"]
    assert out.get("body", "|").split("|")[0] == ""


def test_page_reports_decision_map_prints_its_snapshot_age_from_the_data() -> None:
    page = REPORT_PAGES["decision-map"].read_text(encoding="utf-8")
    assert "D.asof" in page
    assert 'id="snap"' in page
    assert page.count("<script>") == 1
