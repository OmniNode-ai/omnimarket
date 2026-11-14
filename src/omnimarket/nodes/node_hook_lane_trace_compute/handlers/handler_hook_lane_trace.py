# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane trace: one lane's completed calls with their full stored output and a typed label (OMN-24072).

The query joins the lane's PostToolUse events (omninode_internal.claude_hook_events, by its lane column) to the chunks of
each call's stored output (omninode_internal.session_content, by session_id and tool_use_id). Each stored output and exit
code goes through HandlerHookToolOutcome, so a call that printed a refusal and ended with exit code 0 is refused and never
ok. A call with no stored output is counted as no-output and carries no label. Pure: no clock, no I/O, no model; the rules
come from the outcome node's overlay.
"""

from __future__ import annotations

import json
from collections import Counter

from omnimarket.nodes.node_hook_lane_trace_compute.models import (
    EnumLaneCallLabel,
    ModelLaneTraceCall,
    ModelLaneTraceCounts,
    ModelLaneTraceRequest,
    ModelLaneTraceResult,
    ModelLaneTraceRow,
)
from omnimarket.nodes.node_hook_tool_outcome_compute.handlers.handler_hook_tool_outcome import (
    HandlerHookToolOutcome,
    ModelHookToolOutcomeRequest,
)
from omnimarket.nodes.node_hook_tool_outcome_compute.overlay import load_outcome_rules

SCHEMA = "omninode_internal"
EVENTS_TABLE = "claude_hook_events"
CONTENT_TABLE = "session_content"


def build_sql(request: ModelLaneTraceRequest) -> str:
    """The READ ONLY script for the lane's completed calls with their stored output, one 'tc|<json>' line per call.

    One row per call (session, tool_use_id): its newest completion event, the newest `limit` calls of the window,
    oldest first. The request validated the lane name and both bounds, so only a plain token and two UTC stamps appear."""
    r = request
    return (
        "BEGIN READ ONLY;\n"
        "SELECT 'tc|' || json_build_object('emitted_at', to_char(t AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'), 'session_id', session_id, 'tool_use_id', tool_use_id, "
        "'tool_name', tool_name, 'hook_event_name', hook_event_name, 'exit_code', exit_code, 'lane', lane, "
        "'content_kind', kind, 'output', out)::text FROM (\n"
        "  SELECT * FROM (\n"
        "    SELECT DISTINCT ON (e.session_id, e.tool_use_id) e.emitted_at AS t, e.session_id, e.tool_use_id, "
        "e.tool_name, e.hook_event_name, e.exit_code, e.lane, c.kind, c.out\n"
        f"    FROM {SCHEMA}.{EVENTS_TABLE} e\n"
        "    LEFT JOIN LATERAL (SELECT min(sc.content_kind) AS kind, "
        "string_agg(sc.content, '' ORDER BY sc.chunk_index) AS out\n"
        f"      FROM {SCHEMA}.{CONTENT_TABLE} sc WHERE sc.session_id = e.session_id AND sc.tool_use_id = e.tool_use_id\n"
        "        AND sc.content_kind IN ('tool_response', 'tool_error')) c ON true\n"
        f"    WHERE e.lane = '{r.lane}' AND e.hook_event_name IN ('PostToolUse', 'PostToolUseFailure')\n"
        f"      AND e.tool_use_id IS NOT NULL AND e.emitted_at >= '{r.since}' AND e.emitted_at < '{r.until}'\n"
        "    ORDER BY e.session_id, e.tool_use_id, e.emitted_at DESC) d\n"
        f"  ORDER BY t DESC LIMIT {r.limit}) n ORDER BY t, session_id, tool_use_id;\n"
        "COMMIT;\n"
    )


def display_output(output: str) -> list[str]:
    """The stored output as lines. A Bash tool_response is a JSON object: its stdout, then its stderr and an
    interrupted mark when it has them. Any other stored text is printed as it is."""
    try:
        body = json.loads(output)
    except ValueError:
        return output.splitlines() or [""]
    if not isinstance(body, dict) or "stdout" not in body:
        return output.splitlines() or [""]
    lines = str(body.get("stdout") or "").splitlines()
    if body.get("stderr"):
        lines += ["[stderr]", *str(body["stderr"]).splitlines()]
    if body.get("interrupted") is True:
        lines.append("[interrupted]")
    return lines or [""]


class HandlerHookLaneTrace:
    def __init__(self, outcome: HandlerHookToolOutcome | None = None) -> None:
        # Without an injected outcome handler (the runtime builds the handler with no arguments), the rules are read
        # from the outcome node's overlay on first use; an absent overlay refuses by name.
        self._outcome_handler = outcome

    @property
    def _outcome(self) -> HandlerHookToolOutcome:
        if self._outcome_handler is None:
            self._outcome_handler = HandlerHookToolOutcome(load_outcome_rules())
        return self._outcome_handler

    def handle(self, request: ModelLaneTraceRequest) -> ModelLaneTraceResult:
        sql = build_sql(request)
        if request.rows is None:
            return ModelLaneTraceResult(sql=sql)
        calls = tuple(self._label(row) for row in request.rows)
        n = Counter(c.label for c in calls)
        counts = ModelLaneTraceCounts(
            calls=len(calls),
            ok=n[EnumLaneCallLabel.OK],
            failed=n[EnumLaneCallLabel.FAILED],
            refused=n[EnumLaneCallLabel.REFUSED],
            timeout=n[EnumLaneCallLabel.TIMEOUT],
            no_output=n[EnumLaneCallLabel.NO_OUTPUT],
        )
        return ModelLaneTraceResult(
            sql=sql,
            calls=calls,
            counts=counts,
            lines=tuple(self._render(request, calls, counts)),
        )

    def _label(self, row: ModelLaneTraceRow) -> ModelLaneTraceCall:
        if not row.output:
            return ModelLaneTraceCall(row=row, label=EnumLaneCallLabel.NO_OUTPUT)
        failure = row.content_kind == "tool_error"
        result = self._outcome.handle(
            ModelHookToolOutcomeRequest(
                tool_name=row.tool_name,
                hook_event_name=row.hook_event_name,
                tool_response=None if failure else row.output,
                error=row.output if failure else None,
                exit_code=row.exit_code,
            )
        )
        label = EnumLaneCallLabel(result.outcome.value)
        component = (
            result.refusing_component if label is EnumLaneCallLabel.REFUSED else None
        )
        return ModelLaneTraceCall(row=row, label=label, refusing_component=component)

    @staticmethod
    def _render(
        request: ModelLaneTraceRequest,
        calls: tuple[ModelLaneTraceCall, ...],
        counts: ModelLaneTraceCounts,
    ) -> list[str]:
        lines = [
            f"LANE-TRACE lane={request.lane} window={request.since}..{request.until}"
        ]
        for c in calls:
            row = c.row
            label = (
                f"{c.label.value}:{c.refusing_component}"
                if c.refusing_component
                else c.label.value
            )
            exit_code = "-" if row.exit_code is None else str(row.exit_code)
            lines.append(
                f"call {row.emitted_at} lane={row.lane} tool={row.tool_name} label={label} exit={exit_code} "
                f"session={row.session_id[:8]} call={row.tool_use_id[:12]}"
            )
            if row.output:
                lines.extend("    | " + ln for ln in display_output(row.output))
            else:
                lines.append("    | (no stored output)")
        if counts.calls >= request.limit:
            lines.append(
                f"note    newest {request.limit} calls shown (--limit); narrow --since to read earlier ones"
            )
        lines.append(
            f"outcomes calls={counts.calls} ok={counts.ok} failed={counts.failed} refused={counts.refused} "
            f"timeout={counts.timeout} no-output={counts.no_output}"
        )
        return lines
