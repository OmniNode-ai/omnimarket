# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22848: the real wiring of the lane start leader (``build_real``).

Two leaders for one lane and attempt must not both win the CLAIM: the ledger effect's
check-then-append has to run under a lock that is shared across leaders (a flock beside the
ledger), not under a private in-memory lock per leader.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

from omnimarket.models.work_ledger_append.model_work_ledger_append import (
    EnumWorkLedgerAppendStatus,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.handlers.adapter_host_pool import (
    InMemoryLaneHostPool,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.handlers.handler_remote_lane_start import (
    build_real,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.orchestration.core import (
    claim_request_id,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests import (
    lane_start_fixtures as fx,
)

SLOW_WRITER = """\
import sys, time
ledger = sys.argv[1]
row = sys.argv[sys.argv.index("--append") + 1]
time.sleep(0.5)
with open(ledger, "a", encoding="utf-8") as out:
    out.write(row + "\\n")
"""


def test_two_leaders_for_one_attempt_do_not_both_acquire_the_claim(
    tmp_path: Path,
) -> None:
    writer = tmp_path / "writer.py"
    writer.write_text(SLOW_WRITER, encoding="utf-8")
    ledger = tmp_path / "ledger.md"
    ledger.write_text("# fixture ledger\n", encoding="utf-8")
    cfg = fx.settings(ledger_append_argv=[sys.executable, str(writer)])
    req = fx.request(ledger_path=str(ledger))
    handler = build_real(
        cfg,
        InMemoryLaneHostPool(fx.snapshot()),
        stop_requested=lambda: False,
        sleep=lambda _s: None,
        echo=lambda _line: None,
    )
    # Two leaders, each with the collaborators build_real gives it.
    build = handler._build  # noqa: SLF001
    assert build is not None
    leaders = [build(req) for _ in range(2)]
    req_id = claim_request_id(req.lane, req.attempt)
    statuses: list[EnumWorkLedgerAppendStatus] = []
    guard = threading.Lock()

    def claim(leader_index: int) -> None:
        _row, receipt = leaders[leader_index]._append(  # noqa: SLF001
            req, req_id, "CLAIM", [f"lane={req.lane}", f"ticket={req.ticket}"], "claim"
        )
        with guard:
            statuses.append(receipt.status)

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert statuses.count(EnumWorkLedgerAppendStatus.ACCEPTED) == 1, statuses
    claims = [
        line
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if "| CLAIM |" in line
    ]
    assert len(claims) == 1
