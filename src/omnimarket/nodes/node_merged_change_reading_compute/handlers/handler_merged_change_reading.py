# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per merged change: in the build or not, node loaded, last run, last result, and the one missing step.

Pure: the caller reads the build facts and the runtime facts and supplies them with the clock. Every verdict word,
step code and the order of the requirements per run kind is in ``request.config``, which the reader effect loads
from this node's ``contract.yaml`` or from an overlay. A requirement that cannot be decided because its fact group
was unread gives the unread verdict, never the not-running verdict.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from omnimarket.models.model_merged_change_reading import (
    ModelMcrBuildFact,
    ModelMcrChange,
    ModelMcrNodeReading,
    ModelMcrReading,
    ModelMcrRequest,
    ModelMcrRequirement,
    ModelMcrResult,
    ModelMcrRunKind,
    ModelMcrRunner,
    ModelMcrRuntimeFacts,
    ModelMcrSummary,
)

# (met, unread group). met is True or False, or None when the fact group was unread.
_Decision = tuple[bool | None, str]


def _latest(
    runner: ModelMcrRunner, runtime: ModelMcrRuntimeFacts
) -> tuple[datetime | None, str, bool]:
    """The latest evidence of a run for the runner: its time, its result text ("" when none) and whether it succeeded."""
    best: tuple[datetime, str, bool] | None = None
    wanted = set(runner.result_topics)
    for item in runtime.published:
        if item.topic in wanted and (best is None or item.last_at > best[0]):
            best = (item.last_at, item.topic, True)
    if runner.unit:
        for run in runtime.unit_runs:
            if run.unit == runner.unit and (best is None or run.last_run_at > best[0]):
                best = (run.last_run_at, run.result, run.ok)
    return (None, "", True) if best is None else best


class HandlerMergedChangeReading:
    """handle(request) -> result: one reading per merged change."""

    def handle(self, request: ModelMcrRequest) -> ModelMcrResult:
        keys = [(c.repo, c.number) for c in request.changes]
        if len(set(keys)) != len(keys):
            raise ValueError("changes repeats a change")
        config = request.config
        runners = {(r.repo, r.number): r for r in request.runners}
        kinds = {kind.kind: kind for kind in config.run_kinds}
        steps = {item.requirement: item for item in config.requirements}
        builds = {(b.repo, b.number): b for b in request.build_facts}
        readings = tuple(
            self._read(request, change, runners, kinds, steps, builds)
            for change in request.changes
        )
        verdicts = [reading.verdict for reading in readings]
        summary = ModelMcrSummary(
            total=len(readings),
            running=verdicts.count(config.verdict_running),
            not_running=verdicts.count(config.verdict_not_running),
            unread=verdicts.count(config.verdict_unread),
        )
        return ModelMcrResult(
            now=request.now,
            build_started_at=request.build_started_at,
            readings=readings,
            summary=summary,
        )

    def _read(
        self,
        request: ModelMcrRequest,
        change: ModelMcrChange,
        runners: dict[tuple[str, int], ModelMcrRunner],
        kinds: dict[str, ModelMcrRunKind],
        steps: dict[str, ModelMcrRequirement],
        builds: dict[tuple[str, int], ModelMcrBuildFact],
    ) -> ModelMcrReading:
        config = request.config
        key = (change.repo, change.number)
        runner = runners.get(key)
        build = builds.get(key)
        in_build: bool | None = None if build is None else build.in_build
        if runner is None:
            return ModelMcrReading(
                repo=change.repo,
                number=change.number,
                merge_sha=change.merge_sha,
                kind="",
                in_build=in_build,
                nodes=(),
                last_run_at=None,
                last_result="",
                verdict=config.verdict_not_running,
                missing_step=config.no_runner_step,
                next_action=config.no_runner_next_action,
                detail="no runner is declared for this change",
            )
        kind = kinds.get(runner.kind)
        if kind is None:
            raise ValueError(f"unknown run kind {runner.kind!r}")
        runtime = request.runtime
        nodes = self._node_readings(runner, runtime)
        last_at, last_result, last_ok = _latest(runner, runtime)
        checks: dict[str, Callable[[], _Decision]] = {
            "in_build": lambda: self._in_build(request, change, build),
            "node_loaded": lambda: self._node_loaded(runner, runtime),
            "subscribed": lambda: self._subscribed(runner, runtime),
            "ran": lambda: self._ran(request, runner, runtime),
            "result": lambda: (bool(last_result), ""),
            "ok": lambda: (bool(last_result) and last_ok, ""),
        }
        verdict = config.verdict_running
        missing_step = ""
        next_action = ""
        detail = ""
        for name in kind.requires:
            met, unread_group = checks[name]()
            if met is None:
                verdict = config.verdict_unread
                detail = f"{name} unread: {unread_group}"
                break
            if not met:
                item = steps[name]
                verdict = config.verdict_not_running
                missing_step = item.missing_step
                next_action = item.next_action
                detail = f"{name} not met"
                break
        return ModelMcrReading(
            repo=change.repo,
            number=change.number,
            merge_sha=change.merge_sha,
            kind=runner.kind,
            in_build=in_build,
            nodes=nodes,
            last_run_at=last_at,
            last_result=last_result,
            verdict=verdict,
            missing_step=missing_step,
            next_action=next_action,
            detail=detail,
        )

    @staticmethod
    def _in_build(
        request: ModelMcrRequest,
        change: ModelMcrChange,
        build: ModelMcrBuildFact | None,
    ) -> _Decision:
        if build is not None:
            return build.in_build, ""
        if (
            change.repo in request.build_unread
            or f"{change.repo}#{change.number}" in request.build_unread
        ):
            return None, f"build of {change.repo}"
        return False, ""

    @staticmethod
    def _node_loaded(
        runner: ModelMcrRunner, runtime: ModelMcrRuntimeFacts
    ) -> _Decision:
        if "loaded_nodes" in runtime.unread:
            return None, "loaded_nodes"
        if not runner.nodes:
            return False, ""
        loaded = {item.node for item in runtime.loaded_nodes}
        return all(node in loaded for node in runner.nodes), ""

    @staticmethod
    def _subscribed(runner: ModelMcrRunner, runtime: ModelMcrRuntimeFacts) -> _Decision:
        if "subscriptions" in runtime.unread:
            return None, "subscriptions"
        if not runner.trigger_topics or not runner.nodes:
            return False, ""
        joined = {(s.node, s.topic) for s in runtime.subscriptions}
        return (
            all(
                any((node, topic) in joined for topic in runner.trigger_topics)
                for node in runner.nodes
            ),
            "",
        )

    @staticmethod
    def _ran(
        request: ModelMcrRequest, runner: ModelMcrRunner, runtime: ModelMcrRuntimeFacts
    ) -> _Decision:
        started = request.build_started_at
        met = False
        wanted = set(runner.result_topics)
        for item in runtime.published:
            if item.topic in wanted and (started is None or item.last_at >= started):
                met = True
        if runner.unit:
            for run in runtime.unit_runs:
                if run.unit == runner.unit and (
                    started is None or run.last_run_at >= started
                ):
                    met = True
        if met:
            return True, ""
        if not runner.result_topics and not runner.unit:
            if runner.no_caller:
                return False, ""
            if runner.shared_result_topics:
                return (
                    None,
                    "attribution: the runtime log names no publisher on a shared topic",
                )
            return None, "no run evidence is declared for this change"
        unread = [
            group
            for group, needed in (
                ("published", bool(runner.result_topics)),
                ("unit_runs", bool(runner.unit)),
            )
            if needed and group in runtime.unread
        ]
        if unread:
            return None, ",".join(unread)
        return False, ""

    @staticmethod
    def _node_readings(
        runner: ModelMcrRunner, runtime: ModelMcrRuntimeFacts
    ) -> tuple[ModelMcrNodeReading, ...]:
        loaded = {item.node for item in runtime.loaded_nodes}
        last_at, last_result, _ = _latest(runner, runtime)
        loaded_known = "loaded_nodes" not in runtime.unread
        return tuple(
            ModelMcrNodeReading(
                node=node,
                loaded=(node in loaded) if loaded_known else None,
                subscribed_topics=tuple(
                    sorted({s.topic for s in runtime.subscriptions if s.node == node})
                ),
                last_result_topic=last_result,
                last_run_at=last_at,
            )
            for node in runner.nodes
        )
