# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""read_ledger opens no ledger file, and the contract check that says so passes at the head.

The check once grepped the whole source_readers.py for ``path.open``, which the unrelated
``_jsonl`` reader of the landing sources legitimately holds, so the check failed at the head
while read_ledger opened nothing. The check now reads read_ledger's own body.
"""

from __future__ import annotations

import builtins
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_process_health_effect.handlers.handler_process_health_run import (
    load_runtime_io,
)
from omnimarket.nodes.node_process_health_effect.handlers.source_readers import (
    Sources,
    read_ledger,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[5]
CHECK_ID = "dod-omn26664-no-ledger-file-open"
NOW = datetime(2026, 11, 25, 7, 0, tzinfo=UTC)


def _check_command() -> str:
    contract = yaml.safe_load((REPO / "contracts" / "OMN-26664.yaml").read_text())
    item = next(i for i in contract["dod_evidence"] if i["id"] == CHECK_ID)
    return str(item["checks"][0]["check_value"])


def test_read_ledger_opens_no_file(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("read_ledger opened a file")

    io = load_runtime_io()["ledger"]
    monkeypatch.setattr(builtins, "open", refuse)
    monkeypatch.setattr(Path, "open", refuse)
    out = Sources()
    read_ledger(
        io,
        NOW - timedelta(days=3),
        NOW,
        out,
        lambda *_args, **_kwargs: (
            "2026-11-24T10:00:00Z | HOLD | id=h1 | lane=a | a hold",
        ),
        clock=lambda: NOW,
    )
    assert out.reads[-1].status.value == "read"


def test_the_contract_check_passes_at_the_head() -> None:
    done = subprocess.run(
        ["bash", "-c", _check_command()], cwd=REPO, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_contract_check_still_fails_on_a_ledger_file_open() -> None:
    probe = (
        "src=$(mktemp); printf 'def read_ledger(io):\\n    live.is_file()\\n"
        'def read_landing(io):\\n    pass\\n\' > "$src"; '
        + _check_command().replace(
            "src/omnimarket/nodes/node_process_health_effect/handlers/source_readers.py",
            '"$src"',
        )
    )
    done = subprocess.run(
        ["bash", "-c", probe], cwd=REPO, capture_output=True, text=True
    )
    assert done.returncode != 0, "a ledger file open went undetected"
