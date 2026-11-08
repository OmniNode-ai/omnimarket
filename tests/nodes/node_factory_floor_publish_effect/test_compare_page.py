# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 and AC3 of OMN-23018: the compare page is served by the floor's publisher and draws the better and worse lines.

The chain: the simple floor on the registry home, the comparison node's cycle, the publisher's copy to the served
directory, an HTTP read of ``compare.html`` and ``floor-compare.json``, and the page's own script under node drawing the
served comparison for each of the 1, 7 and 30 day switches. The failing control is a comparison with a planted gap, which
the page draws as unread and never as better or worse.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from functools import partial
from http.client import HTTPConnection
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from threading import Thread

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    HandlerFactoryFloorPublish,
    load_runtime,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishRequest,
)
from omnimarket.nodes.node_floor_compare_effect.handlers.handler_floor_compare import (
    HandlerFloorCompare,
)
from omnimarket.nodes.node_floor_compare_effect.models.model_floor_compare import (
    ModelFloorCompareRequest,
)
from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
    LocalSsh,
    _home,
    _overlay,
)
from tests.nodes.node_floor_compare_effect.support import (
    NOW,
    RT,
    seed,
    served_floor,
    snap,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_factory_floor_publish_effect"
)
COMPARE_SRC = (NODE_DIR / "page/compare.html").read_text(encoding="utf-8")
SIMPLE_SRC = (NODE_DIR / "page/simple.html").read_text(encoding="utf-8")


def _compare(home: Path, history: tuple) -> None:
    simple = home / RT.simple_relpath
    simple.parent.mkdir(parents=True, exist_ok=True)
    simple.write_text(json.dumps(served_floor()), encoding="utf-8")
    seed(home, *history)
    result = HandlerFloorCompare(sink=InMemoryEmissionSink(), clock=lambda: NOW).run(
        ModelFloorCompareRequest(home=str(home), now=NOW)
    )
    assert result.ok


def _publish_and_read(tmp_path: Path, names: tuple[str, ...]) -> dict[str, bytes]:
    site = tmp_path / "site"
    site.mkdir(exist_ok=True)
    with HTTPServer(
        ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(site))
    ) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            HandlerFactoryFloorPublish(LocalSsh()).handle(
                ModelFactoryFloorPublishRequest(
                    home=str(tmp_path / "home"),
                    overlay=_overlay(tmp_path),
                    skip_refresh=True,
                    now=NOW,
                ),
                environ={},
            )
            served: dict[str, bytes] = {}
            for name in names:
                conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                conn.request("GET", f"/{name}")
                response = conn.getresponse()
                served[name] = response.read() if response.status == 200 else b""
                conn.close()
            return served
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_the_publisher_serves_the_compare_page_and_the_comparison(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    _compare(home, (snap(1, {"open_prs": 12, "merges": 4}), snap(7, {"open_prs": 20})))
    served = _publish_and_read(
        tmp_path, ("compare.html", "floor-compare.json", "publish-status.json")
    )
    assert served["compare.html"] == (NODE_DIR / "page/compare.html").read_bytes()
    doc = json.loads(served["floor-compare.json"])
    assert doc["schema_id"] == "omni.floor-compare/1"
    assert [w["days"] for w in doc["windows"]] == [1, 7, 30]
    day = doc["windows"][0]
    line = next(x for x in day["better"] if x["key"] == "open_prs")
    assert (line["old"], line["new"], line["better_is"]) == (
        12.0,
        8.0,
        "lower_is_better",
    )
    assert json.loads(served["publish-status.json"])["floor_compare"] == "published"


def test_error_no_comparison_file_means_no_data_is_published_and_the_status_says_absent(
    tmp_path: Path,
) -> None:
    _home(tmp_path)
    served = _publish_and_read(
        tmp_path, ("compare.html", "floor-compare.json", "publish-status.json")
    )
    assert served["compare.html"]  # the page is served, and draws its own failure
    assert served["floor-compare.json"] == b""
    assert json.loads(served["publish-status.json"])["floor_compare"] == "absent"


def test_the_site_root_carries_a_link_to_the_compare_page() -> None:
    assert 'href="compare.html"' in SIMPLE_SRC
    assert load_runtime().names.compare_page == "compare.html"
    assert load_runtime().names.compare_data == "floor-compare.json"


def test_the_page_reads_the_served_comparison_and_has_the_three_way_switch() -> None:
    assert 'DATA = "floor-compare.json"' in COMPARE_SRC
    assert "data-days" in COMPARE_SRC
    assert "aria-pressed" in COMPARE_SRC
    for needed in ("old_text", "new_text", "direction_text", "words", "counts.unread"):
        assert needed in COMPARE_SRC, needed


HARNESS = textwrap.dedent(
    """
    const fs = require("fs");
    const [pagePath, filesJson, hash, nowIso] = process.argv.slice(1);
    const files = JSON.parse(filesJson);
    const src = fs.readFileSync(pagePath, "utf8");
    const script = /<script>([\\s\\S]*?)<\\/script>/.exec(src)[1];
    const els = {};
    const mk = id => ({id, innerHTML: "", querySelectorAll: () => [], setAttribute() {}, addEventListener() {}});
    globalThis.document = {title: "", getElementById: id => els[id] || (els[id] = mk(id))};
    globalThis.window = {addEventListener() {}};
    globalThis.location = {hash};
    const now = Date.parse(nowIso);
    Date.now = () => now;
    globalThis.setInterval = () => 0;
    globalThis.fetch = async url => (url in files
      ? {ok: true, status: 200, json: async () => JSON.parse(files[url])}
      : {ok: false, status: 404, json: async () => ({})});
    (async () => {
      new Function(script)();
      await new Promise(r => setTimeout(r, 50));
      process.stdout.write(JSON.stringify({html: els.app.innerHTML, title: document.title}));
    })();
    """
)


def _render(doc: bytes | None, hash_: str) -> dict:
    node = shutil.which("node")
    assert node, "the compare page render test requires Node.js"
    files = {} if doc is None else {"floor-compare.json": doc.decode()}
    out = subprocess.run(
        [
            node,
            "-e",
            HARNESS,
            str(NODE_DIR / "page/compare.html"),
            json.dumps(files),
            hash_,
            NOW.isoformat(),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(out.stdout)


@pytest.mark.parametrize(
    ("hash_", "label", "better_key"),
    [("", "1 day", "open_prs"), ("#7", "7 days", "open_prs"), ("#30", "30 days", None)],
)
def test_the_switch_draws_better_and_worse_lines_with_old_new_and_direction(
    tmp_path: Path, hash_: str, label: str, better_key: str | None
) -> None:
    home = _home(tmp_path)
    _compare(
        home,
        (
            snap(1, {"open_prs": 12, "merges": 4}),
            snap(7, {"open_prs": 20, "merges": 1}),
        ),
    )
    doc = (home / RT.output_relpath).read_bytes()
    drawn = _render(doc, hash_)
    assert f"than {label} ago" in drawn["title"]
    html = drawn["html"]
    assert 'aria-pressed="true">' + label in html
    if better_key:
        assert "Old: <strong>" in html
        assert "New: <strong>8 open</strong>" in html
        assert "Lower is better" in html
        assert "Got better" in html
    else:
        assert "Nothing got better." in html
        assert "Nothing got worse." in html


def test_failing_control_a_planted_gap_is_drawn_unread_never_better_or_worse(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    _compare(home, (snap(1, {"open_prs": 12}),))  # 7 days ago is a gap
    html = _render((home / RT.output_relpath).read_bytes(), "#7")["html"]
    assert "no reading 7 days ago" in html
    assert "Nothing got better." in html
    assert "Nothing got worse." in html


def test_a_missing_comparison_draws_a_failure_not_an_empty_page() -> None:
    assert "cannot be read" in _render(None, "")["html"]
