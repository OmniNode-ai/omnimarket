# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The settings CLI end to end on the in-memory path (OMN-24059).

Exit 0 with the host's argv, bound and flag as one JSON line; no row for the host is exit 10 and the flag
still printed; a missing or malformed overlay is exit 69 with the overlay named on stderr. The printed keys
are the seam the plugin's drain_map reads (an exact key set).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_ledger_rows_loader_settings_effect.runtime_rows_loader_settings import (
    EXIT_HOST_ROW_MISSING,
    EXIT_REFUSED,
    EXIT_RESOLVED,
    main,
)
from tests.unit.nodes.node_ledger_rows_loader_settings_effect.conftest_support import (
    HEAD,
    host_row,
    write_overlays,
)

pytestmark = pytest.mark.unit

PAYLOAD_KEYS = {
    "status",
    "reason",
    "file_fallback_allowed",
    "argv",
    "timeout_s",
    "host",
}


def run(
    env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    *,
    host: str = "Hp-Gen8.lab.local",
) -> tuple[int, dict[str, object], str]:
    code = main(
        ["--registry-root", "/registry/root"], environ=env, hostname=lambda: host
    )
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def test_golden_a_host_row_prints_the_command_the_bound_and_the_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload, err = run(write_overlays(tmp_path / "ovl"), capsys)
    assert code == EXIT_RESOLVED
    assert err == ""
    assert set(payload) == PAYLOAD_KEYS
    assert payload == {
        "status": "resolved",
        "reason": "host-row-found",
        "file_fallback_allowed": True,
        "argv": [
            "/registry/root/.onex-dispatch-venv/bin/python",
            "-m",
            "omnimarket.nodes.node_ledger_rows_loader_effect",
        ],
        "timeout_s": 45,
        "host": "Hp-Gen8",
    }


@pytest.mark.parametrize("allowed", ["true", "false"])
def test_error_a_host_with_no_row_is_exit_10_with_the_flag_and_the_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], allowed: str
) -> None:
    env = write_overlays(tmp_path / "ovl", allowed=allowed)
    code, payload, err = run(env, capsys, host="other")
    assert code == EXIT_HOST_ROW_MISSING
    assert payload["argv"] is None
    assert payload["file_fallback_allowed"] is (allowed == "true")
    assert "reason=loader-host-row-missing" in err


def test_error_a_missing_hosts_overlay_is_exit_69_naming_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = write_overlays(tmp_path / "ovl")
    (tmp_path / "ovl" / "ledger_rows_loader_hosts" / "overlay.yaml").unlink()
    code, payload, err = run(env, capsys)
    assert code == EXIT_REFUSED
    assert payload["status"] == "refused"
    assert payload["argv"] is None
    assert "ledger_rows_loader_hosts" in err
    assert "status=refused" in err
    assert payload["file_fallback_allowed"] is True


def test_error_a_missing_loader_overlay_is_exit_69_naming_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload, err = run(
        {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "nothing-here")}, capsys
    )
    assert code == EXIT_REFUSED
    assert payload["file_fallback_allowed"] is None
    assert "node_ledger_rows_loader_effect" in err


def test_error_a_malformed_hosts_overlay_is_exit_69_naming_the_fields(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = write_overlays(tmp_path / "ovl", hosts=HEAD.replace("45", "0") + host_row())
    code, _payload, err = run(env, capsys)
    assert code == EXIT_REFUSED
    assert "ledger_rows_loader_hosts" in err
    assert "timeout_seconds" in err


def test_the_event_is_journalled_when_the_loader_overlays_journal_variable_is_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    journal = tmp_path / "journal.jsonl"
    env = {
        **write_overlays(tmp_path / "ovl"),
        "ONEX_LEDGER_ROWS_LOADER_JOURNAL": str(journal),
    }
    run(env, capsys)
    run(env, capsys, host="other")
    kinds = [json.loads(line)["kind"] for line in journal.read_text().splitlines()]
    assert kinds == ["answered", "refused"]
