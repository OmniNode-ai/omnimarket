# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor page lists agent messages newest first, filters them, shows one thread in order, marks every sender unverified and shows the refused counter."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

PAGE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_factory_floor_publish_effect/page/floor/index.html"
)
TOPIC = "onex.snapshot.projection.agent-message.v1"

HARNESS = textwrap.dedent(
    """
    const fs = require("fs");
    const [pagePath, rowsJson, stepsJson] = process.argv.slice(1);
    const rows = JSON.parse(rowsJson), steps = JSON.parse(stepsJson);
    const script = /<script>([\\s\\S]*?)<\\/script>/.exec(fs.readFileSync(pagePath, "utf8"))[1];
    const els = {}, listeners = {};
    const mk = id => ({id, innerHTML: "", textContent: "", hidden: false, value: "", dataset: {}, style: {},
      className: "", tabIndex: 0, title: "", children: [], classList: {add() {}, remove() {}},
      querySelectorAll: () => [], setAttribute() {}, replaceChildren() {}, appendChild(c) { this.children.push(c); },
      focus() {}, addEventListener(ev, fn) { (listeners[id + ":" + ev] = listeners[id + ":" + ev] || []).push(fn); }});
    globalThis.document = {title: "", getElementById: id => els[id] || (els[id] = mk(id)), addEventListener() {},
      querySelectorAll: () => [], createElement: () => mk("created"), documentElement: {classList: {add() {}}}};
    globalThis.window = {addEventListener() {}, matchMedia: () => ({matches: false}), scrollTo() {}, scrollY: 0};
    globalThis.location = {hash: "", search: ""};
    globalThis.history = {replaceState() {}};
    globalThis.localStorage = {getItem: () => null, setItem() {}};
    globalThis.getComputedStyle = () => ({getPropertyValue: () => "1"});
    globalThis.setInterval = () => 0;
    const urls = [];
    globalThis.fetch = async url => {
      urls.push(url);
      if (url.includes("agent-message")) {
        if (rows === null) return {ok: false, status: 503, json: async () => ({})};
        return {ok: true, status: 200, json: async () => ({topic: "x", row_count: rows.length, rows})};
      }
      return {ok: false, status: 404, json: async () => ({})};
    };
    const wait = ms => new Promise(r => setTimeout(r, ms));
    (async () => {
      new Function(script).call(globalThis);
      await wait(150);
      const shots = [];
      const snap = () => { const o = {}; for (const [k, v] of Object.entries(els)) o[k] = v.innerHTML + "|" + v.textContent; shots.push(o); };
      snap();
      for (const step of steps) {
        for (const [id, value] of Object.entries(step)) { els[id].value = value; for (const fn of listeners[id + ":change"] || []) fn({target: els[id]}); }
        await wait(30);
        snap();
      }
      console.log(JSON.stringify({shots, urls}));
    })();
    """
)


def _node() -> str:
    node = shutil.which("node") or "/data/lab-run/tools/node/bin/node"
    if not Path(node).exists():
        pytest.skip("node is not installed")
    return node


def _row(offset: int, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source_partition": 0,
        "source_offset": offset,
        "status": "ok",
        "refusal_reason": None,
        "refusal_detail": None,
        "from_agent": "claude-h201",
        "to_agent": "codex-h053",
        "thread": "proof-2217",
        "kind": "message",
        "platform": "claude",
        "text": f"text {offset}",
        "sent_at": f"2026-11-24T22:17:{offset:02d}+00:00",
        "broker_at": f"2026-11-24T22:17:{offset:02d}+00:00",
        "sender_verified": False,
    }
    base.update(over)
    return base


def _draw(
    rows: list[dict[str, Any]] | None, steps: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    done = subprocess.run(
        [
            _node(),
            "-e",
            HARNESS,
            str(PAGE),
            json.dumps(rows),
            json.dumps(steps or []),
        ],
        capture_output=True,
        check=False,
        timeout=60,
        env={**os.environ},
    )
    assert done.returncode == 0, done.stderr.decode()
    out: dict[str, Any] = json.loads(done.stdout.decode().strip().splitlines()[-1])
    return out


def _body(shot: dict[str, str]) -> str:
    return shot["msgBody"]


def test_the_section_reads_the_projection_of_the_agent_message_topic() -> None:
    out = _draw([_row(1)])
    assert any(TOPIC in u for u in out["urls"])


def test_the_two_proof_messages_show_as_rows_newest_first() -> None:
    rows = [
        _row(1, text="first synthetic"),
        _row(2, text="second synthetic", thread="proof-2217"),
    ]
    body = _body(_draw(rows)["shots"][0])
    assert "first synthetic" in body
    assert "second synthetic" in body
    assert body.index("second synthetic") < body.index("first synthetic")


def test_the_section_filters_by_agent_thread_and_platform() -> None:
    rows = [
        _row(
            1,
            from_agent="a-claude",
            to_agent="b-codex",
            thread="t1",
            platform="claude",
            text="m1",
        ),
        _row(
            2,
            from_agent="b-codex",
            to_agent="a-claude",
            thread="t1",
            platform="codex",
            text="m2",
        ),
        _row(
            3,
            from_agent="c-glm",
            to_agent="d-gemini",
            thread="t2",
            platform="glm",
            text="m3",
        ),
    ]
    shots = _draw(
        rows,
        [
            {"msgAgent": "c-glm"},
            {"msgAgent": "", "msgThread": "t1"},
            {"msgThread": "", "msgPlatform": "codex"},
        ],
    )["shots"]
    by_agent, by_thread, by_platform = (_body(s) for s in shots[1:4])
    assert "m3" in by_agent
    assert "m1" not in by_agent
    assert "m2" not in by_agent
    assert "m1" in by_thread
    assert "m2" in by_thread
    assert "m3" not in by_thread
    assert "m2" in by_platform
    assert "m1" not in by_platform
    assert "m3" not in by_platform


def test_an_agent_filter_matches_the_sender_and_the_recipient() -> None:
    rows = [
        _row(1, from_agent="x", to_agent="y", text="out"),
        _row(2, from_agent="y", to_agent="x", text="in"),
    ]
    shots = _draw(rows, [{"msgAgent": "x"}])["shots"]
    assert "out" in _body(shots[1])
    assert "in</" in _body(shots[1])


def test_one_thread_is_shown_oldest_first() -> None:
    rows = [_row(i, thread="t1", text=f"step-{i}") for i in (3, 1, 2)]
    shots = _draw(rows, [{"msgThread": "t1"}])["shots"]
    body = _body(shots[1])
    assert body.index("step-1") < body.index("step-2") < body.index("step-3")


def test_every_sender_is_marked_unverified_while_the_proof_is_not_deployed() -> None:
    body = _body(_draw([_row(1), _row(2)])["shots"][0])
    assert body.count("sender unverified") == 2
    assert "sender verified" not in body.replace("sender unverified", "")


def test_a_row_the_projection_marks_verified_is_shown_verified_and_only_then() -> None:
    body = _body(_draw([_row(1, sender_verified=True), _row(2)])["shots"][0])
    assert body.count("sender unverified") == 1
    assert "sender verified" in body


def test_the_refused_counter_counts_dropped_unreadable_and_refused() -> None:
    refused = [
        _row(
            10,
            status="refused",
            refusal_reason="dropped",
            from_agent=None,
            to_agent=None,
            thread=None,
            kind=None,
            platform=None,
            text=None,
            sent_at=None,
        ),
        _row(
            11,
            status="refused",
            refusal_reason="unreadable",
            from_agent=None,
            to_agent=None,
            thread=None,
            kind=None,
            platform=None,
            text="garbage",
            sent_at=None,
        ),
        _row(
            12,
            status="refused",
            refusal_reason="refused",
            refusal_detail="platform: bad",
            from_agent=None,
            to_agent=None,
            thread=None,
            kind=None,
            platform=None,
            text=None,
            sent_at=None,
        ),
    ]
    shot = _draw([_row(1), *refused])["shots"][0]
    counter = shot["msgRefused"]
    assert "3" in counter
    assert "dropped" in counter
    assert "unreadable" in counter
    assert "refused" in counter
    # a refused row is not drawn as a message, and the readable one still is
    assert "text 1" in _body(shot)
    assert "garbage" not in _body(shot)


def test_the_counter_says_zero_when_nothing_was_refused_and_never_blanks() -> None:
    shot = _draw([_row(1)])["shots"][0]
    assert "0" in shot["msgRefused"]


def test_a_failed_read_says_so_instead_of_showing_an_empty_list() -> None:
    shot = _draw(None)["shots"][0]
    assert "could not be read" in shot["msgNote"] + shot["msgBody"]
    assert "No messages" not in _body(shot)


def test_message_text_is_escaped() -> None:
    body = _body(_draw([_row(1, text="<img src=x onerror=1>")])["shots"][0])
    assert "<img" not in body
    assert "&lt;img" in body
