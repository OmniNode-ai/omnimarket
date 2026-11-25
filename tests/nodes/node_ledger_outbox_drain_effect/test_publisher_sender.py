# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The publisher adapter runs the declared command and answers its receipt; the node runs through its entry point."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from omnimarket.nodes.node_ledger_outbox_drain_effect.adapters.adapter_publisher_sender import (
    PublisherSender,
    lane_of,
)

from .support import NOON, REQ, fresh_row

pytestmark = pytest.mark.unit


def _script(tmp_path: Path, answer: str) -> Path:
    script = tmp_path / "publish.py"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "argv = sys.argv[1:]\n"
        "opts = dict(zip(argv[::2], argv[1::2]))\n"
        "rows = sys.stdin.read()\n"
        f"open({str(tmp_path / 'seen.json')!r}, 'w').write(json.dumps({{'opts': opts, 'rows': rows}}))\n"
        f"print(json.dumps({{'request_id': opts['--request-id'], 'status': {answer!r}}}))\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_the_sender_passes_the_request_id_the_lane_of_the_first_row_and_the_rows(
    tmp_path: Path,
) -> None:
    sender = PublisherSender(
        [str(_script(tmp_path, "accepted"))], lane="dflt", host="h1"
    )
    result = sender(fresh_row(NOON), REQ)
    assert result.exit_code == 0
    assert (result.receipt or {}).get("status") == "accepted"
    seen = json.loads((tmp_path / "seen.json").read_text(encoding="utf-8"))
    assert seen["opts"]["--request-id"] == REQ
    assert seen["opts"]["--lane"] == "host-lane"
    assert seen["opts"]["--host"] == "h1"
    assert seen["rows"] == fresh_row(NOON)


def test_the_lane_falls_back_to_the_configured_one() -> None:
    assert lane_of("2026-11-09T12:00:00Z | MSG | from=a | to=b | x\n", "dflt") == "a"
    assert lane_of("no row here\n", "dflt") == "dflt"


def test_an_unstartable_publisher_answers_pending(tmp_path: Path) -> None:
    sender = PublisherSender([str(tmp_path / "missing")], lane="l", host="h")
    assert sender("x", REQ).exit_code == 75


def test_an_empty_argv_prefix_is_refused() -> None:
    with pytest.raises(ValueError, match="empty"):
        PublisherSender([], lane="l", host="h")
