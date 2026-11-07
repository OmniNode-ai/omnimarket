# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the pull request check in shadow over the declared repositories and append one row.

For every open pull request of a declared repository the check builds the graph at the pull
request's head and at its base, names the modules the head adds that no root reaches, and notes
the allowlist entries past their date. The shadow logs what it would refuse and decides nothing:
the result has ``holds_merge`` false whatever the verdicts are, and the row says ``gate=none``. A
pull request whose refs cannot be read is ``indeterminate`` and is counted, never passed. A run
that cannot read the pull requests at all fails with a typed failure and a row that says so.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.models.model_code_graph import (
    EnumGraphFault,
    EnumShadowOutcome,
    ModelShadowCommand,
    ModelShadowPullRequest,
    ModelShadowResult,
    ModelShadowVerdict,
)
from omnimarket.nodes.node_code_graph_effect.handlers.graph_compute import (
    GraphRules,
    check_added,
)
from omnimarket.nodes.node_code_graph_effect.overlay import (
    ModelCodeGraphOverlay,
    ModelCodeGraphShadow,
)
from omnimarket.nodes.node_code_graph_effect.protocols import (
    ProtocolPullRequestSource,
    ProtocolRepoSource,
    PullRequestSourceError,
    RepoSourceError,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    reason_token,
    stamp_text,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
_ROW_TYPE = "STATUS"
_ROW_SOURCE_CELL = "src=code-graph-shadow"


def load_shadow_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


def _cell(text: str) -> str:
    return text.replace("|", "/").replace(" ", "_")


class HandlerCodeGraphShadow:
    """Check the open pull requests of the overlay's shadow repositories, in shadow."""

    def __init__(
        self,
        *,
        overlay: ModelCodeGraphOverlay,
        host: str,
        source: ProtocolRepoSource,
        pull_requests: ProtocolPullRequestSource,
        appender: ProtocolReadingAppender | None,
        clock: Callable[[], datetime],
    ) -> None:
        self._overlay = overlay
        self._host = host
        self._source = source
        self._pull_requests = pull_requests
        self._appender = appender
        self._clock = clock

    def _shadow(self) -> ModelCodeGraphShadow:
        shadow = self._overlay.shadow
        if shadow is None:
            raise ValueError("the overlay declares no shadow")
        return shadow

    def _verdict(
        self,
        pull_request: ModelShadowPullRequest,
        rules: GraphRules,
        today: datetime,
    ) -> ModelShadowVerdict:
        try:
            base_snapshot = self._source.snapshot(
                pull_request.repo, pull_request.base_ref, rules.wanted
            )
            head_snapshot = self._source.snapshot(
                pull_request.repo, pull_request.head_sha, rules.wanted
            )
        except RepoSourceError as exc:
            return ModelShadowVerdict(
                repo=pull_request.repo,
                number=pull_request.number,
                head=pull_request.head_sha,
                outcome=EnumShadowOutcome.INDETERMINATE,
                reason=f"ref-unreadable:{reason_token(str(exc))}",
            )
        checked = check_added(
            base_snapshot,
            head_snapshot,
            rules,
            allowlist=self._overlay.allowlist,
            today=today.date(),
        )
        outcome = (
            EnumShadowOutcome.WOULD_PASS
            if checked.passed
            else EnumShadowOutcome.WOULD_REFUSE
        )
        return ModelShadowVerdict(
            repo=pull_request.repo,
            number=pull_request.number,
            head=pull_request.head_sha,
            outcome=outcome,
            added=checked.added,
            flagged=checked.flagged,
            expired=checked.expired,
        )

    def handle(self, command: ModelShadowCommand) -> ModelShadowResult:
        now = self._clock().astimezone(UTC)
        shadow = self._shadow()
        if command.write and self._appender is None:
            return ModelShadowResult(
                correlation_id=command.correlation_id,
                status="failed",
                fault=command.fault,
                repos=shadow.repos,
                failure="no-appender-for-a-writing-run",
            )
        rules = GraphRules(
            roots=self._overlay.graph_roots,
            contract_links=command.fault is not EnumGraphFault.DROP_CONTRACT_LINKS,
        )
        verdicts: list[ModelShadowVerdict] = []
        skipped = 0
        try:
            if command.fault is EnumGraphFault.UNREADABLE_REPO:
                raise PullRequestSourceError("injected-unreadable-pull-requests")
            for repo in shadow.repos:
                found = self._pull_requests.open_pull_requests(repo)
                skipped += max(0, len(found) - shadow.max_pull_requests)
                verdicts.extend(
                    self._verdict(pull_request, rules, now)
                    for pull_request in found[: shadow.max_pull_requests]
                )
        except PullRequestSourceError as exc:
            return self._publish(
                command,
                now,
                ModelShadowResult(
                    correlation_id=command.correlation_id,
                    status="failed",
                    fault=command.fault,
                    repos=shadow.repos,
                    failure=f"pull-requests-unreadable:{reason_token(str(exc))}",
                ),
                skipped=skipped,
            )
        return self._publish(
            command,
            now,
            ModelShadowResult(
                correlation_id=command.correlation_id,
                status="completed",
                fault=command.fault,
                repos=shadow.repos,
                verdicts=tuple(verdicts),
            ),
            skipped=skipped,
        )

    def _publish(
        self,
        command: ModelShadowCommand,
        now: datetime,
        result: ModelShadowResult,
        *,
        skipped: int,
    ) -> ModelShadowResult:
        check_hash = self._overlay.check_hash()
        heads = ",".join(f"{v.repo}#{v.number}@{v.head[:12]}" for v in result.verdicts)
        key = _sha(
            "|".join(
                (self._host, stamp_text(now), check_hash, heads, command.fault.value)
            )
        )[:32]
        content = _sha(
            json.dumps(
                [
                    result.status,
                    [(v.repo, v.number, v.outcome.value) for v in result.verdicts],
                ],
                sort_keys=True,
            )
        )[:32]
        if not command.write:
            return result.model_copy(update={"row_key": key, "ledger": "not-written"})
        assert self._appender is not None
        publish = self._overlay.publish
        shadow = self._shadow()
        refused = [
            f"{v.repo}#{v.number}:{path}"
            for v in result.verdicts
            if v.outcome is EnumShadowOutcome.WOULD_REFUSE
            for path in (*v.flagged, *v.expired)
        ]
        shown = refused[: publish.row_path_limit]
        more = len(refused) - len(shown)
        cells = [
            f"lane={publish.lane}",
            _ROW_SOURCE_CELL,
            f"actor={publish.actor}",
            "model=none",
            f"host={self._host}",
            f"drill={'no' if command.fault is EnumGraphFault.NONE else 'yes'}",
            f"fault={command.fault.value}",
            "gate=none",
            f"outcome={'read' if result.status == 'completed' else 'source-unreadable'}",
            f"repos={','.join(result.repos)}",
            f"checked={len(result.verdicts)}",
            f"would_refuse={result.count(EnumShadowOutcome.WOULD_REFUSE)}",
            f"would_pass={result.count(EnumShadowOutcome.WOULD_PASS)}",
            f"indeterminate={result.count(EnumShadowOutcome.INDETERMINATE)}",
            f"skipped={skipped}",
            f"refused={_cell(','.join(shown)) if shown else 'none'}"
            + (f",+{more}-more" if more else ""),
            f"probed_at={stamp_text(now)}",
            f"published_at={stamp_text(now)}",
            f"max_age={shadow.shadow_max_age_seconds}",
            f"key={key}",
            f"content={content}",
            f"check_hash={check_hash[:12]}",
            f"correlation={_cell(command.correlation_id)}",
            _clean(
                "shadow: logs what the check would refuse and holds no merge"
                + (f"; failure: {result.failure}" if result.failure else "")
            ),
        ]
        row = f"{stamp_text(now)} | {_ROW_TYPE} | " + " | ".join(cells)
        appended = self._appender.append(
            row + "\n", str(uuid.uuid5(uuid.NAMESPACE_URL, f"{key}|{content}"))
        )
        if not appended.ok:
            why = f"append-failed:{appended.status}"
            return result.model_copy(
                update={
                    "status": "failed",
                    "row_key": key,
                    "ledger": why,
                    "row": row,
                    "failure": result.failure
                    or f"{why}:{reason_token(appended.message or 'no-message')}",
                }
            )
        return result.model_copy(
            update={
                "row_key": key,
                "ledger": "written"
                if appended.status == "accepted"
                else "already-written",
                "row": row,
            }
        )


def shadow_events_of(
    result: ModelShadowResult, topics: dict[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The events a shadow run produces, in order, all under its correlation id."""
    events: list[tuple[str, dict[str, object]]] = [
        (
            topics["code_graph_shadow_pr_checked"],
            {
                "correlation_id": result.correlation_id,
                "repo": verdict.repo,
                "number": verdict.number,
                "head": verdict.head,
                "outcome": verdict.outcome.value,
                "flagged": len(verdict.flagged),
                "expired": len(verdict.expired),
            },
        )
        for verdict in result.verdicts
    ]
    final: dict[str, object] = {
        "correlation_id": result.correlation_id,
        "checked": len(result.verdicts),
        "would_refuse": result.count(EnumShadowOutcome.WOULD_REFUSE),
        "would_pass": result.count(EnumShadowOutcome.WOULD_PASS),
        "indeterminate": result.count(EnumShadowOutcome.INDETERMINATE),
        "holds_merge": result.holds_merge,
        "row_key": result.row_key,
        "ledger": result.ledger,
    }
    if result.status == "failed":
        final["failure"] = result.failure
        events.append((topics["code_graph_shadow_failed"], final))
    else:
        events.append((topics["code_graph_shadow_completed"], final))
    return events


def answer_shadow_command(
    payload: bytes, handler: HandlerCodeGraphShadow, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One shadow command message in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a command is answered with a typed failure event, never dropped.
    """
    try:
        command = ModelShadowCommand.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {"correlation_id": "unknown", "failure": f"bad-payload:{fields}"}
        return [(topics["code_graph_shadow_failed"], json.dumps(failure).encode())]
    return [
        (topic, json.dumps(body, sort_keys=True).encode())
        for topic, body in shadow_events_of(handler.handle(command), topics)
    ]
