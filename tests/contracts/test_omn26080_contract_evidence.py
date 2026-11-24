# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Test and run the readback assertions for this ticket's live ledger evidence."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

LANE = "act1940-c"
PRS = frozenset(
    {
        "overlays#572",
        "omnimarket#6298",
        "omnimarket#5752",
        "omnimarket#6231",
        "omnimarket#5910",
    }
)
INSTALLED = re.compile(r"h201:[0-9a-f]{40}")


def ledger_rows(answer: dict[str, Any]) -> list[tuple[str, dict[str, str]]]:
    assert answer["status"] == "ok", "the ledger read was refused"
    assert not answer.get("next_cursor"), "the ledger read was incomplete"
    rows = []
    for row in answer["rows"]:
        cells = row["raw_row"].splitlines()[0].split(" | ")
        rows.append(
            (
                row["row_type"],
                dict(cell.split("=", 1) for cell in cells[2:] if "=" in cell),
            )
        )
    return rows


def assert_dispositions(rows: list[tuple[str, dict[str, str]]]) -> None:
    found: set[str] = set()
    for kind, fields in rows:
        if kind != "STATUS" or fields.get("lane") != LANE:
            continue
        reading = (
            INSTALLED.fullmatch(fields.get("installed", ""))
            and fields.get("observed")
            and fields.get("read_at", "").endswith("Z")
        )
        if (
            reading
            or fields.get("blocked") in {"access", "owner", "not-merged"}
            or fields.get("ci-only")
        ):
            found.add(fields.get("pr", ""))
    assert found >= PRS, f"missing STATUS dispositions for {sorted(PRS - found)}"


def assert_no_restart_receipt(rows: list[tuple[str, dict[str, str]]]) -> None:
    assert all(
        fields.get("restart") != "true"
        for _, fields in rows
        if fields.get("lane") == LANE or fields.get("status") == "needs-dev-window"
    ), "lane or branch installer registered a restart"
    assert any(
        kind == "TERMINAL"
        and fields.get("lane") == LANE
        and fields.get("shared-dev-restarts") == "0"
        and fields.get("manual-host-edits") == "0"
        for kind, fields in rows
    ), "missing terminal counts for the lane"
    assert any(
        kind == "STATUS"
        and fields.get("status") == "needs-dev-window"
        and fields.get("restart") == "false"
        and INSTALLED.fullmatch(fields.get("installed", ""))
        and fields.get("installer_receipt", "").split(":", 1)[0]
        not in {"", "none", "existing", "existing-runtime-build-provenance"}
        for kind, fields in rows
    ), "missing branch installer receipt with restart disabled"


def test_status_dispositions_are_attributed_and_complete() -> None:
    import pytest

    answer = {
        "status": "ok",
        "rows": [
            {
                "row_type": "STATUS",
                "raw_row": (
                    f"2026-11-19T20:00:00Z | STATUS | lane={LANE} | "
                    f"pr={pr} | blocked=owner"
                ),
            }
            for pr in sorted(PRS)
        ],
    }
    rows = ledger_rows(answer)
    assert_dispositions(rows)
    with pytest.raises(AssertionError, match="missing STATUS dispositions"):
        assert_dispositions(rows[:-1])
    rows[-1][1]["lane"] = "other-lane"
    with pytest.raises(AssertionError, match="missing STATUS dispositions"):
        assert_dispositions(rows)


def test_branch_install_evidence_requires_a_receipt_and_restart_disabled() -> None:
    import pytest

    terminal = {"lane": LANE, "shared-dev-restarts": "0", "manual-host-edits": "0"}
    install = {
        "status": "needs-dev-window",
        "restart": "false",
        "installed": "h201:" + "a" * 40,
        "installer_receipt": "branch-receipt",
    }
    rows = [("TERMINAL", terminal), ("STATUS", install)]
    assert_no_restart_receipt(rows)
    with pytest.raises(AssertionError, match="missing branch installer receipt"):
        assert_no_restart_receipt(rows[:1])
    install["installer_receipt"] = "none:no-branch-install"
    with pytest.raises(AssertionError, match="missing branch installer receipt"):
        assert_no_restart_receipt(rows)
    install["restart"] = "true"
    with pytest.raises(AssertionError, match="registered a restart"):
        assert_no_restart_receipt(rows)


if __name__ == "__main__":
    live_rows = ledger_rows(json.load(sys.stdin))
    assert_dispositions(live_rows)
    assert_no_restart_receipt(live_rows)
