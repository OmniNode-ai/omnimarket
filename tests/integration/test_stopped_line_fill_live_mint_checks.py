# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three checks of the live mint and lane start (plan task T8, acceptance criterion 5), run on fixtures.

The live run needs T11 and T13 merged. Its checks are written here exactly as the criterion writes them and run, with
the real ``jq`` and ``awk``, against fixtures: a matching run, three mismatching ones, a ledger holding an unrelated
concurrent append and a ``T0.10`` row beside the one CLAIM, and one with two new CLAIM rows for the key. The same
checks run unchanged on the files the live run saves (``payload.json``, ``issue.json``, ``ledger.before`` and
``ledger.after`` of each step).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "stopped_line_fill" / "fixtures" / "live_mint"
KEY = (FIXTURES / "KEY").read_text().strip()

ROWS = (
    'rows() { awk -F\' [|] \' -v k="key=$K" -v kind="$2" '
    '\'(kind == "" || $2 == kind) { for (i = 3; i <= NF; i++) if ($i == k) { print; next } }\' "$1"; }'
)

# Check (i): the read-back carries the payload's parent, every criterion verbatim and the key line, and has no
# project, no delegation label and no pillar.
CHECK_I = (
    "jq -e --slurpfile p payload.json '$p[0] as $q | .parent == $q.parent and .project == null and .pillar == null "
    'and ([.labels[] | ascii_downcase | contains("delegation")] | any | not) '
    "and ([$q.criteria[] as $c | (.description | contains($c))] | all) "
    'and (.description | split("\\n") | any(. == $q.floor_key_line))\' issue.json'
)
# Check (ii): after the refusing step the rows of the key are unchanged.
CHECK_II = "diff <(rows refuse/ledger.before) <(rows refuse/ledger.after)"
# Check (iii): after the posix step exactly one new CLAIM row carries the key, and it names the minted ticket.
CHECK_III = (
    "new=$(diff <(rows posix/ledger.before CLAIM) <(rows posix/ledger.after CLAIM) | sed -n 's/^> //p'); "
    '[ "$(printf \'%s\\n\' "$new" | grep -c .)" -eq 1 ] && '
    "printf '%s\\n' \"$new\" | awk -F' [|] ' -v t=\"ticket=$(jq -r .id issue.json)\" "
    "'$2 == \"CLAIM\" { for (i = 3; i <= NF; i++) if ($i == t) ok = 1 } END { exit !ok }'"
)


def run_check(case: str, check: str) -> int:
    """Run one check in the case's directory with ``K`` set to the key; the exit code is the verdict."""
    script = f"set -o pipefail; {ROWS}; K='{KEY}'; {check}"
    done = subprocess.run(
        ["bash", "-c", script],
        cwd=FIXTURES / case,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode


@pytest.mark.parametrize(
    "check", [CHECK_I, CHECK_II, CHECK_III], ids=["i", "ii", "iii"]
)
def test_live_mint_check_fixtures_matching_run_passes_every_check(check: str) -> None:
    assert run_check("match", check) == 0


@pytest.mark.parametrize(
    "case", ["mismatch-parent", "mismatch-delegation-label", "mismatch-key"]
)
def test_live_mint_check_fixtures_mismatching_read_back_fails_check_i(
    case: str,
) -> None:
    assert run_check(case, CHECK_I) != 0


def test_live_mint_check_fixtures_wrong_key_also_fails_check_iii() -> None:
    assert run_check("mismatch-key", CHECK_III) != 0


def test_live_mint_check_fixtures_concurrent_append_and_t0_10_row_do_not_move_the_rows() -> (
    None
):
    """The match case's ledgers gained an unrelated append and a ``T0.10`` row; check (ii) and (iii) still pass."""
    after = (FIXTURES / "match" / "refuse" / "ledger.after").read_text()
    assert f"key={KEY}0" in after  # the T0.10 beside the T0.1
    assert after != (FIXTURES / "match" / "refuse" / "ledger.before").read_text()
    assert run_check("match", CHECK_II) == 0
    assert run_check("match", CHECK_III) == 0


def test_live_mint_check_fixtures_two_new_claim_rows_fail_check_iii() -> None:
    assert run_check("two-claims", CHECK_III) != 0


def test_live_mint_check_fixtures_a_refusing_step_that_wrote_a_row_fails_check_ii() -> (
    None
):
    assert run_check("refuse-wrote-a-row", CHECK_II) != 0


def test_live_mint_check_fixtures_a_claim_for_another_ticket_fails_check_iii() -> None:
    assert run_check("claim-for-another-ticket", CHECK_III) != 0


def test_live_mint_check_fixtures_matching_payload_is_what_the_payload_compute_builds() -> (
    None
):
    """The matching fixture's payload and read-back are the T11 compute's own output for the first task of the replay."""
    import json

    from omnimarket.nodes.node_floor_task_ticket_payload_compute.handlers.handler_floor_task_ticket_payload import (
        HandlerFloorTaskTicketPayload,
    )
    from omnimarket.nodes.node_floor_task_ticket_payload_compute.models import (
        ModelFloorTaskTicketPayloadRequest,
    )

    from tests.integration.stopped_line_fill.support import plan_texts

    line_id, task_id = KEY.split(":")[1:]
    built = HandlerFloorTaskTicketPayload().handle(
        ModelFloorTaskTicketPayloadRequest(
            line_id=line_id, task_id=task_id, plan_text=plan_texts()[line_id]
        )
    )
    assert built.payload is not None
    payload = json.loads((FIXTURES / "match" / "payload.json").read_text())
    issue = json.loads((FIXTURES / "match" / "issue.json").read_text())
    assert payload["parent"] == built.payload.parent == issue["parent"]
    assert payload["floor_key_line"] == f"Floor key: {built.payload.floor_key}"
    assert issue["description"] == built.payload.description
    assert set(issue) == {
        "id",
        "state",
        "parent",
        "project",
        "labels",
        "pillar",
        "description",
    }


def test_live_mint_check_fixtures_tools_exist() -> None:
    for tool in ("bash", "jq", "awk", "diff", "sed", "grep"):
        assert shutil.which(tool), tool
