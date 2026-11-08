# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the code graph of every declared repository, judge it, and append one reading row.

It writes code graph rows only, never a gate, a stop or an admission. A run is WORKING when the
graph agrees with the frozen sample at or above its bar and reaches every known-used control;
otherwise NOT_WORKING with the misses named. A repository that cannot be read fails the run, and
the row says NOT_WORKING with the outcome ``source-unreadable``: a graph that could not be built is
not working. A run with an injected fault is a drill, and its row carries ``drill=yes`` so that no
operational reading reads it.
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
    ModelCodeGraphCommand,
    ModelCodeGraphResult,
    ModelRepoSnapshot,
    ModelSampleOutcome,
)
from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_code_graph_effect.handlers.graph_compute import (
    GraphRules,
    build_graph,
    judge_sample,
)
from omnimarket.nodes.node_code_graph_effect.overlay import ModelCodeGraphOverlay
from omnimarket.nodes.node_code_graph_effect.protocols import (
    ProtocolRepoSource,
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
_ROW_SOURCE_CELL = "src=code-graph"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


def _cell(text: str) -> str:
    """A cell value: no pipe, no space."""
    return text.replace("|", "/").replace(" ", "_")


class HandlerCodeGraph:
    """Run the graph over the overlay's repositories."""

    def __init__(
        self,
        *,
        overlay: ModelCodeGraphOverlay,
        host: str,
        source: ProtocolRepoSource,
        appender: ProtocolReadingAppender | None,
        clock: Callable[[], datetime],
    ) -> None:
        self._overlay = overlay
        self._host = host
        self._source = source
        self._appender = appender
        self._clock = clock

    def rules(self, fault: EnumGraphFault = EnumGraphFault.NONE) -> GraphRules:
        return GraphRules(
            roots=self._overlay.graph_roots,
            contract_links=fault is not EnumGraphFault.DROP_CONTRACT_LINKS,
            entry_points=fault is not EnumGraphFault.DROP_ENTRY_POINTS,
        )

    def _snapshots(
        self, rules: GraphRules, fault: EnumGraphFault
    ) -> list[ModelRepoSnapshot]:
        snapshots: list[ModelRepoSnapshot] = []
        for position, repo in enumerate(self._overlay.repos):
            if position == 0 and fault is EnumGraphFault.UNREADABLE_REPO:
                raise RepoSourceError(f"injected-unreadable:{repo.name}")
            try:
                snapshots.append(
                    self._source.snapshot(repo.name, repo.ref, rules.wanted)
                )
            except RepoSourceError as exc:
                raise RepoSourceError(f"{repo.name}:{exc}") from None
        return snapshots

    def handle(self, command: ModelCodeGraphCommand) -> ModelCodeGraphResult:
        now = self._clock().astimezone(UTC)
        if command.write and self._appender is None:
            return ModelCodeGraphResult(
                correlation_id=command.correlation_id,
                status="failed",
                verdict=EnumReadingVerdict.UNKNOWN,
                outcome=EnumReadingOutcome.SOURCE_UNREADABLE,
                reasons=("no-appender-for-a-writing-run",),
                fault=command.fault,
                failure="no-appender-for-a-writing-run",
            )
        rules = self.rules(command.fault)
        try:
            snapshots = self._snapshots(rules, command.fault)
        except RepoSourceError as exc:
            why = f"repo-unreadable:{reason_token(str(exc))}"
            return self._publish(
                command,
                now,
                ModelCodeGraphResult(
                    correlation_id=command.correlation_id,
                    status="failed",
                    verdict=EnumReadingVerdict.NOT_WORKING,
                    outcome=EnumReadingOutcome.SOURCE_UNREADABLE,
                    reasons=(why,),
                    fault=command.fault,
                    failure=why,
                ),
            )
        graph = build_graph(snapshots, rules)
        sample = judge_sample(graph, self._overlay.sample)
        reasons = self._reasons(sample)
        return self._publish(
            command,
            now,
            ModelCodeGraphResult(
                correlation_id=command.correlation_id,
                status="completed",
                verdict=EnumReadingVerdict.WORKING
                if reasons == ("ok",)
                else EnumReadingVerdict.NOT_WORKING,
                outcome=EnumReadingOutcome.READ,
                reasons=reasons,
                fault=command.fault,
                repos=tuple(
                    graph.repo_graph(snapshot.repo, snapshot.head)
                    for snapshot in snapshots
                ),
                sample=sample,
            ),
        )

    def _reasons(self, sample: ModelSampleOutcome) -> tuple[str, ...]:
        bar = self._overlay.sample.min_agree
        reasons: list[str] = []
        if sample.agree < bar:
            reasons.append(f"below-bar:{sample.agree}-of-{sample.total}-bar-{bar}")
        if sample.controls_reached < sample.controls_total:
            reasons.append(
                f"controls-unreached:{sample.controls_total - sample.controls_reached}"
            )
        if not reasons:
            return ("ok",)
        return (*reasons, *(reason_token(miss) for miss in sample.misses))

    def _publish(
        self,
        command: ModelCodeGraphCommand,
        now: datetime,
        result: ModelCodeGraphResult,
    ) -> ModelCodeGraphResult:
        overlay_hash = self._overlay.overlay_hash()
        heads = ",".join(f"{repo.repo}@{repo.head[:12]}" for repo in result.repos)
        key = _sha(
            "|".join(
                (self._host, stamp_text(now), overlay_hash, heads, command.fault.value)
            )
        )[:32]
        content = _sha(
            json.dumps(
                [
                    result.verdict.value,
                    result.outcome.value,
                    list(result.reasons),
                    heads,
                ],
                sort_keys=True,
            )
        )[:32]
        if not command.write:
            return result.model_copy(update={"row_key": key, "ledger": "not-written"})
        assert self._appender is not None
        publish = self._overlay.publish
        limit = publish.row_path_limit
        shown = result.reasons[:limit] if limit else result.reasons[:1]
        more = len(result.reasons) - len(shown)
        totals = {
            "modules": sum(repo.modules for repo in result.repos),
            "reachable": sum(repo.reachable for repo in result.repos),
            "unreachable": sum(len(repo.unreachable) for repo in result.repos),
            "test_only": sum(len(repo.test_only) for repo in result.repos),
        }
        sample = result.sample
        cells = [
            f"lane={publish.lane}",
            _ROW_SOURCE_CELL,
            f"actor={publish.actor}",
            "model=none",
            f"host={self._host}",
            f"drill={'no' if command.fault is EnumGraphFault.NONE else 'yes'}",
            f"fault={command.fault.value}",
            f"verdict={result.verdict.value}",
            f"outcome={result.outcome.value}",
            f"reason={_cell(','.join(shown))}" + (f",+{more}-more" if more else ""),
            f"repos={len(result.repos)}",
            *(f"{name}={value}" for name, value in totals.items()),
            f"sample={sample.agree}/{sample.total}" if sample else "sample=none",
            f"sample_bar={self._overlay.sample.min_agree}",
            f"controls={sample.controls_reached}/{sample.controls_total}"
            if sample
            else "controls=none",
            f"sample_hash={self._overlay.sample.frozen_hash[:16]}",
            f"heads={_sha(heads)[:16] if heads else 'none'}",
            f"probed_at={stamp_text(now)}",
            f"published_at={stamp_text(now)}",
            f"max_age={publish.publish_max_age_seconds}",
            f"key={key}",
            f"content={content}",
            f"overlay_hash={overlay_hash[:12]}",
            f"correlation={_cell(command.correlation_id)}",
            _clean(f"injected fault: {self._overlay.injected_fault}"),
        ]
        row = f"{stamp_text(now)} | {_ROW_TYPE} | " + " | ".join(cells)
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{key}|{content}"))
        appended = self._appender.append(row + "\n", request_id)
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


def events_of(
    result: ModelCodeGraphResult, topics: dict[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The events a run produces, in order, all under its correlation id."""
    events: list[tuple[str, dict[str, object]]] = [
        (
            topics["code_graph_run_started"],
            {"correlation_id": result.correlation_id, "fault": result.fault.value},
        )
    ]
    events.extend(
        (
            topics["code_graph_repo_read"],
            {
                "correlation_id": result.correlation_id,
                "repo": repo.repo,
                "head": repo.head,
                "modules": repo.modules,
                "reachable": repo.reachable,
                "unreachable": len(repo.unreachable),
            },
        )
        for repo in result.repos
    )
    final: dict[str, object] = {
        "correlation_id": result.correlation_id,
        "verdict": result.verdict.value,
        "outcome": result.outcome.value,
        "reasons": list(result.reasons),
        "row_key": result.row_key,
        "ledger": result.ledger,
    }
    if result.status == "failed":
        final["failure"] = result.failure
        events.append((topics["code_graph_run_failed"], final))
    else:
        events.append((topics["code_graph_run_completed"], final))
    return events


def answer_command(
    payload: bytes, handler: HandlerCodeGraph, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One command message in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a command is answered with a typed failure event, never dropped.
    """
    try:
        command = ModelCodeGraphCommand.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {"correlation_id": "unknown", "failure": f"bad-payload:{fields}"}
        return [(topics["code_graph_run_failed"], json.dumps(failure).encode())]
    return [
        (topic, json.dumps(body, sort_keys=True).encode())
        for topic, body in events_of(handler.handle(command), topics)
    ]
