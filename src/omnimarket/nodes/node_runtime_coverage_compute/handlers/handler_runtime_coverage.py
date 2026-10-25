# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Classify every declared command subscription; no I/O or broker imports."""

from omnimarket.models.runtime_coverage import (
    ModelCommandCoverage,
    ModelRuntimeCoverageResult,
    ModelRuntimeCoverageSnapshot,
)


class HandlerRuntimeCoverageCompute:
    def handle(
        self, request: ModelRuntimeCoverageSnapshot
    ) -> ModelRuntimeCoverageResult:
        rows: list[ModelCommandCoverage] = []
        for command in request.commands:
            if request.error or command.node in request.unreadable_nodes:
                rows.append(
                    ModelCommandCoverage(
                        node=command.node,
                        topic=command.topic,
                        status="FAIL",
                        detail=request.error
                        or "broker membership unreadable for this node",
                    )
                )
                continue
            serving = {
                (m.host, m.runtime_id)
                for m in request.members
                if m.node == command.node and command.topic in m.topics
            }
            count = len(serving)
            hosts = tuple(sorted({host for host, _ in serving}))
            runtime_ids = tuple(sorted({runtime for _, runtime in serving}))
            status = "OK" if count >= 2 else "FAIL"
            rows.append(
                ModelCommandCoverage(
                    node=command.node,
                    topic=command.topic,
                    status=status,
                    serving_runtimes=count,
                    runtime_ids=runtime_ids,
                    hosts=hosts,
                    detail=f"{command.node}: {count} serving runtime(s); hosts={','.join(hosts) or 'none'}; required=2",
                )
            )
        error = request.error or (
            "no declared command subscriptions" if not rows else ""
        )
        return ModelRuntimeCoverageResult(
            bus_lane=request.bus_lane,
            broker=request.broker,
            observed_at=request.observed_at,
            status="FAIL"
            if error or request.registry_errors or any(r.status == "FAIL" for r in rows)
            else "OK",
            commands=tuple(rows),
            error=error,
            registry_errors=request.registry_errors,
        )
