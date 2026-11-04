"""The waiting tick shares the exhaustive renderer and records its acting STATUS row.

A refused row write never hides a rendered item. Source failures return UNKNOWN and
never write a successful acting row for a list that was not read.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile

from omnimarket.nodes.node_operator_item_render_compute import (
    NodeOperatorItemRenderCompute,
)
from omnimarket.protocols.protocol_operator_items import ProtocolOperatorItemRenderer

from .models import ModelOperatorWaiting, ModelOperatorWaitingRequest

ACTOR = "script:operator-waiting-tick"
QUESTION_CELL_KEY = "question="
ASK_TEXT_CUT = 160


def ask_question(cells: list[str]) -> str:
    """What a MSG to the operator asks, from the ledger's own ask field, never from its words (OMN-16177).

    A row is an ask when it carries a non-empty ``question="..."`` cell: ``/omni:operator-ruling`` ask and
    ``onex_ledger.py row MSG --question`` write it, and the ledger grammar has no other marker of an ask on a MSG.
    The reports the orchestrator logs after acting (SENT-ON-BEHALF, MANUAL-ENQUEUE, ESCALATION-*) are written
    without it by design and return an empty string. ``cells`` are the row's pipe-separated cells, the leading
    stamp and the row type first; the text is cut to ``ASK_TEXT_CUT`` characters for a list line.
    """
    for cell in cells[2:]:
        if cell.startswith(QUESTION_CELL_KEY):
            text = " ".join(cell[len(QUESTION_CELL_KEY) :].strip().strip('"').split())
            return (
                text
                if len(text) <= ASK_TEXT_CUT
                else text[: ASK_TEXT_CUT - 3].rstrip() + "..."
            )
    return ""


class HandlerOperatorWaitingTick:
    def __init__(self, renderer: ProtocolOperatorItemRenderer | None = None) -> None:
        self._renderer = (
            renderer if renderer is not None else NodeOperatorItemRenderCompute()
        )

    def handle(self, request: ModelOperatorWaitingRequest) -> ModelOperatorWaiting:
        rendered = self._renderer.handle(request.items)
        lines = list(rendered.lines)
        if not rendered.known:
            return ModelOperatorWaiting(lines=lines, exit_code=1)
        if not rendered.count:
            return ModelOperatorWaiting(lines=lines)
        report = request.items.report
        assert report is not None
        assert request.items.config is not None
        opened = [
            item
            for item in report.asks
            if item.state not in request.items.config.closed_states
        ]
        tickets = len({ticket for item in opened for ticket in item.tickets})
        new_count = sum(
            item.ask_id in request.items.new
            or item.state in request.items.config.new_states
            for item in opened
        )
        text = (
            f"operator-waiting tick surfaced {rendered.count} open items, {new_count} new, "
            f"oldest {rendered.oldest}, across {tickets} tickets"
        )
        why = self._append(
            request, text, rendered.oldest or "none", tickets, rendered.count, new_count
        )
        if why is not None:
            lines.append(f"NOTE operator-waiting: the tick row was not recorded: {why}")
        return ModelOperatorWaiting(
            lines=lines, not_recorded=why, exit_code=int(why is not None)
        )

    @staticmethod
    def _append(
        request: ModelOperatorWaitingRequest,
        text: str,
        oldest: str,
        tickets: int,
        count: int,
        new_count: int,
    ) -> str | None:
        cells = {
            "actor": ACTOR,
            "open": count,
            "new": new_count,
            "tickets": tickets,
            "oldest": oldest,
        }
        cmd = [
            sys.executable,
            str(request.resolver),
            "row",
            "STATUS",
            "--ledger",
            request.ledger,
            "--lane",
            request.lane,
        ]
        for key, value in cells.items():
            cmd += ["--kv", f"{key}={value}"]
        try:
            # The ledger CLI requires a detail directory even for successful writes; hosted runners have no ONEX_REGISTRY_ROOT.
            with tempfile.TemporaryDirectory(
                prefix="operator-waiting-row-"
            ) as detail_dir:
                res = subprocess.run(
                    [*cmd, "--detail-dir", detail_dir, "--text", text],
                    capture_output=True,
                    text=True,
                    timeout=request.timeout,
                    check=False,
                )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return f"{type(exc).__name__}: {exc}"[:200]
        if res.returncode != 0:
            why = (res.stdout.strip() or res.stderr.strip()).splitlines()[-1:] or [
                "no output"
            ]
            return f"the row writer exited {res.returncode}: {why[0][:200]}"
        return None
