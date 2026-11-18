# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The text a baseline-cluster ticket carries: the description the creation gate admits, and the occurrence comment.

The description names the lane that created it, the gate it answers, the cluster's signature (the
exact line, plus the legacy two-line form the old reporter filed, so old and new reports match),
what failed and when, and the one acceptance criterion the cluster's own tests falsify on dev.
"""

from __future__ import annotations

from omnimarket.nodes.node_baseline_cluster_ticket_effect.handlers.cluster_signature import (
    legacy_lines,
    signature_line,
)
from omnimarket.nodes.node_baseline_cluster_ticket_effect.models.model_baseline_cluster_ticket import (
    ModelBaselineClusterReportRequest,
)

ACCEPTANCE_HEADER = "## Acceptance criteria"
AC1_TEMPLATE = (
    "- [ ] AC1: the tests of cluster {cluster} in {repo} pass on dev"
    " -- falsifier: run the cluster's test module on dev and read it fail"
)


def ticket_description(
    request: ModelBaselineClusterReportRequest, previous: str | None
) -> str:
    """What a created ticket carries: the lines the ticket-creation gate admits, then the occurrence."""
    legacy_repo, legacy_cluster = legacy_lines(request.repo, request.cluster)
    lines = [
        f"created-by-lane: {request.requested_by_lane}",
        "",
        f"Gate: {request.gate}",
        f"Diagnosis: {request.diagnosis}",
        signature_line(request.repo, request.cluster),
        legacy_repo,
        legacy_cluster,
        *(f"- `{nodeid}`" for nodeid in request.examples[: request.example_limit]),
        f"Failing: {request.failing_count}",
        f"Observed: {request.observed_at}",
        f"Run: {request.run_url}",
    ]
    if previous is not None:
        lines.append(f"Previous ticket (closed): {previous}")
    lines += [
        "",
        ACCEPTANCE_HEADER,
        "",
        AC1_TEMPLATE.format(cluster=request.cluster, repo=request.repo),
    ]
    return "\n".join(lines) + "\n"


def occurrence_comment(request: ModelBaselineClusterReportRequest) -> str:
    """The one comment an open ticket gets for a new occurrence of the cluster."""
    examples = ", ".join(
        f"`{nodeid}`" for nodeid in request.examples[: request.example_limit]
    )
    return (
        f"Still failing: {request.failing_count} failing test(s)"
        f" at {request.observed_at} (run {request.run_url}): {examples}"
    )
