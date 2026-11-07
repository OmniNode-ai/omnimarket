# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read every workflow definition of the repositories and resolve each routing scenario, without I/O (OMN-22948).

R5 of the base-first plan closes the shared runner group once every definition is hosted, moved to a
lab job node or deleted. This handler is the complete definition read that step needs: the caller
reads each repository's workflow files (and the typed route list and the live Actions variables) and
injects them; the handler expands each job's ``runs-on`` into its scenarios, resolves each one and
counts what is left.

A scenario is one value a job's ``runs-on`` can take: one literal label, one matrix value, one literal
of an expression, one seam variable, one computed context, one runner group, or one reusable-workflow
call. It resolves on a GitHub-hosted label (by its definition), or through a declared route (moved to a
lab job node, retired with the runners, or moving to hosted). A declared route is counted as pending
execution: the definition still selects a lab label until a pull request changes it. An expected
repository that was not read is one unresolved scenario of its own, so an incomplete read never
reports zero.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from functools import cache

import yaml

from omnimarket.nodes.node_workflow_routing_scan_compute.models import (
    EnumRoutingResolution,
    EnumRoutingRouteKind,
    EnumRoutingSelection,
    EnumRoutingVia,
    ModelRepositorySummary,
    ModelRoutingScanPolicy,
    ModelRoutingScenario,
    ModelScannedRepository,
    ModelStaleRoute,
    ModelWorkflowRoutingScanRequest,
    ModelWorkflowRoutingScanResult,
)

_Selection = tuple[EnumRoutingSelection, str]
_Make = Callable[[str, EnumRoutingSelection, str], ModelRoutingScenario]
_FileScenarios = Callable[[str, str], list[ModelRoutingScenario]]
_ROUTE_RESOLUTION = {
    EnumRoutingRouteKind.MOVES_TO_HOSTED: EnumRoutingResolution.HOSTED,
    EnumRoutingRouteKind.LAB_JOB_NODE: EnumRoutingResolution.MOVED_TO_NODE,
    EnumRoutingRouteKind.RETIRES_WITH_RUNNERS: EnumRoutingResolution.DELETED,
}


@cache
def _re(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def _hosted(label: object, policy: ModelRoutingScanPolicy) -> bool:
    return isinstance(label, str) and bool(
        _re(policy.hosted_label_pattern).match(label.strip())
    )


def _label(label: str, policy: ModelRoutingScanPolicy) -> _Selection:
    kind = (
        EnumRoutingSelection.HOSTED_LABEL
        if _hosted(label, policy)
        else EnumRoutingSelection.LAB_LABEL
    )
    return (kind, label.strip())


def _seam(
    name: str,
    repository: str,
    variables: Mapping[tuple[str, str], str],
    policy: ModelRoutingScanPolicy,
) -> list[_Selection]:
    if name not in policy.hosted_seam_variables:
        return [(EnumRoutingSelection.UNDECLARED_VARIABLE, f"vars.{name}")]
    value = variables.get((repository, name))
    if value is None:
        return [(EnumRoutingSelection.UNREAD_SEAM_VARIABLE, f"vars.{name}")]
    try:
        labels = json.loads(value)
    except ValueError:
        labels = None
    if isinstance(labels, list) and labels:
        if all(_hosted(item, policy) for item in labels):
            return [(EnumRoutingSelection.SEAM_VARIABLE, f"vars.{name}={value}")]
        return [
            (EnumRoutingSelection.LAB_LABEL, f"vars.{name}={value}"),
        ]
    return [(EnumRoutingSelection.LAB_LABEL, f"vars.{name}={value}")]


def _expression(
    expression: str,
    repository: str,
    variables: Mapping[tuple[str, str], str],
    policy: ModelRoutingScanPolicy,
) -> list[_Selection]:
    body = expression.strip()
    if not (body.startswith("${{") and body.endswith("}}")):
        return [(EnumRoutingSelection.COMPUTED, expression.strip())]
    body = body[3:-2].strip()
    found: list[_Selection] = []
    for name in dict.fromkeys(_re(policy.variable_reference_pattern).findall(body)):
        found.extend(_seam(name, repository, variables, policy))
    rest = _re(policy.variable_reference_pattern).sub("''", body)
    rest = _re(policy.non_label_call_pattern).sub("(", rest)
    if policy.self_hosted_marker in rest.lower():
        found.append((EnumRoutingSelection.LAB_LABEL, policy.self_hosted_marker))
    if _re(policy.unprovable_context_pattern).search(rest):
        found.append((EnumRoutingSelection.COMPUTED, body))
    for array in _re(policy.json_array_literal_pattern).findall(rest):
        found.extend(_label(item, policy) for item in re.findall(r'"([^"]*)"', array))
    found.extend(
        _label(item, policy)
        for item in _re(policy.returned_literal_pattern).findall(rest)
    )
    return list(dict.fromkeys(found)) or [(EnumRoutingSelection.COMPUTED, body)]


def _matrix_values(job: Mapping[object, object], key: str) -> object:
    strategy = job.get("strategy")
    matrix = strategy.get("matrix") if isinstance(strategy, Mapping) else None
    return matrix.get(key) if isinstance(matrix, Mapping) else None


def _selections(
    runs_on: object,
    job: Mapping[object, object],
    repository: str,
    variables: Mapping[tuple[str, str], str],
    policy: ModelRoutingScanPolicy,
) -> list[_Selection]:
    if isinstance(runs_on, str):
        if "${{" not in runs_on:
            return [_label(runs_on, policy)]
        reference = _re(policy.matrix_reference_pattern).match(runs_on.strip())
        if reference is not None:
            values = _matrix_values(job, reference.group(1))
            if isinstance(values, list) and values:
                return _selections(values, job, repository, variables, policy)
        return _expression(runs_on, repository, variables, policy)
    if isinstance(runs_on, list):
        found: list[_Selection] = []
        for item in runs_on:
            found.extend(_selections(item, job, repository, variables, policy))
        return list(dict.fromkeys(found))
    if isinstance(runs_on, Mapping):
        if "group" in runs_on:
            return [(EnumRoutingSelection.RUNNER_GROUP, str(runs_on["group"]))]
        return _selections(
            runs_on.get("labels", []), job, repository, variables, policy
        )
    return []


class HandlerWorkflowRoutingScan:
    """Expand every job of every injected workflow into scenarios and resolve each."""

    def handle(
        self, request: ModelWorkflowRoutingScanRequest
    ) -> ModelWorkflowRoutingScanResult:
        policy = request.policy
        variables = {(v.repository, v.name): v.value for v in request.variables}
        routes = {(r.repository, r.workflow, r.job): r.kind for r in request.routes}
        by_name = {repo.name: repo for repo in request.repositories}
        sources = {
            (repo.name, file.path): file.source
            for repo in request.repositories
            for file in repo.workflows
        }
        memo: dict[tuple[str, str], list[ModelRoutingScenario]] = {}
        visiting: set[tuple[str, str]] = set()

        def file_scenarios(repository: str, path: str) -> list[ModelRoutingScenario]:
            key = (repository, path)
            if key in memo:
                return memo[key]
            visiting.add(key)
            memo[key] = self._file(
                repository,
                path,
                sources[key],
                variables,
                routes,
                policy,
                sources,
                file_scenarios,
                visiting,
            )
            visiting.discard(key)
            return memo[key]

        scenarios: list[ModelRoutingScenario] = []
        summaries: list[ModelRepositorySummary] = []
        unread = tuple(
            name for name in request.expected_repositories if name not in by_name
        )
        extra = tuple(
            repo.name
            for repo in request.repositories
            if repo.name not in request.expected_repositories
        )
        for name in unread:
            scenario = ModelRoutingScenario(
                repository=name,
                workflow="",
                job="",
                selection=EnumRoutingSelection.UNREAD_REPOSITORY,
                detail="the repository's workflow definitions were not read",
                resolution=EnumRoutingResolution.UNRESOLVED,
                via=EnumRoutingVia.NONE,
            )
            scenarios.append(scenario)
            summaries.append(
                ModelRepositorySummary(
                    name=name,
                    visibility="unknown",
                    read=False,
                    workflows=0,
                    scenarios=1,
                    unresolved=1,
                    pending_execution=0,
                )
            )
        for repo in request.repositories:
            mine = [
                scenario
                for file in repo.workflows
                for scenario in file_scenarios(repo.name, file.path)
            ]
            scenarios.extend(mine)
            summaries.append(self._summary(repo, mine))

        stale = self._stale(request, scenarios)
        return ModelWorkflowRoutingScanResult(
            complete=not unread,
            repositories_expected=len(request.expected_repositories),
            repositories_read=len(request.repositories) - len(extra),
            repositories_unread=unread,
            repositories_extra=extra,
            workflows_read=sum(len(repo.workflows) for repo in request.repositories),
            scenario_count=len(scenarios),
            unresolved_count=sum(
                1 for s in scenarios if s.resolution is EnumRoutingResolution.UNRESOLVED
            ),
            pending_execution_count=sum(
                1 for s in scenarios if s.via is EnumRoutingVia.ROUTE
            ),
            scenarios=tuple(scenarios),
            repositories=tuple(summaries),
            stale_routes=stale,
        )

    @staticmethod
    def _summary(
        repo: ModelScannedRepository, mine: list[ModelRoutingScenario]
    ) -> ModelRepositorySummary:
        return ModelRepositorySummary(
            name=repo.name,
            visibility=repo.visibility,
            read=True,
            workflows=len(repo.workflows),
            scenarios=len(mine),
            unresolved=sum(
                1 for s in mine if s.resolution is EnumRoutingResolution.UNRESOLVED
            ),
            pending_execution=sum(1 for s in mine if s.via is EnumRoutingVia.ROUTE),
        )

    @staticmethod
    def _stale(
        request: ModelWorkflowRoutingScanRequest,
        scenarios: list[ModelRoutingScenario],
    ) -> tuple[ModelStaleRoute, ...]:
        read = {repo.name for repo in request.repositories}
        selecting = {
            (s.repository, s.workflow, s.job)
            for s in scenarios
            if s.selection is not EnumRoutingSelection.HOSTED_LABEL
        }
        return tuple(
            ModelStaleRoute(
                repository=route.repository, workflow=route.workflow, job=route.job
            )
            for route in request.routes
            if route.repository in read
            and (route.repository, route.workflow, route.job) not in selecting
        )

    @staticmethod
    def _file(
        repository: str,
        path: str,
        source: str,
        variables: Mapping[tuple[str, str], str],
        routes: Mapping[tuple[str, str, str], EnumRoutingRouteKind],
        policy: ModelRoutingScanPolicy,
        sources: Mapping[tuple[str, str], str],
        file_scenarios: _FileScenarios,
        visiting: set[tuple[str, str]],
    ) -> list[ModelRoutingScenario]:
        def make(
            job: str, selection: EnumRoutingSelection, detail: str
        ) -> ModelRoutingScenario:
            resolution, via = EnumRoutingResolution.UNRESOLVED, EnumRoutingVia.NONE
            if selection in (
                EnumRoutingSelection.HOSTED_LABEL,
                EnumRoutingSelection.SEAM_VARIABLE,
            ):
                resolution, via = (
                    EnumRoutingResolution.HOSTED,
                    EnumRoutingVia.DEFINITION,
                )
            elif (kind := routes.get((repository, path, job))) is not None:
                resolution, via = _ROUTE_RESOLUTION[kind], EnumRoutingVia.ROUTE
            return ModelRoutingScenario(
                repository=repository,
                workflow=path,
                job=job,
                selection=selection,
                detail=detail,
                resolution=resolution,
                via=via,
            )

        try:
            document = yaml.safe_load(source)
        except yaml.YAMLError as exc:
            return [
                make("", EnumRoutingSelection.UNPARSEABLE, str(exc).splitlines()[0])
            ]
        if not isinstance(document, Mapping):
            return [make("", EnumRoutingSelection.UNPARSEABLE, "not a mapping")]
        jobs = document.get("jobs")
        if not isinstance(jobs, Mapping):
            return []
        out: list[ModelRoutingScenario] = []
        for job_id, job in jobs.items():
            if not isinstance(job, Mapping):
                continue
            name = str(job_id)
            uses = job.get("uses")
            if isinstance(uses, str):
                out.append(
                    _caller(
                        name,
                        uses,
                        repository,
                        policy,
                        sources,
                        file_scenarios,
                        visiting,
                        make,
                    )
                )
                continue
            out.extend(
                make(name, kind, detail)
                for kind, detail in _selections(
                    job.get("runs-on"), job, repository, variables, policy
                )
            )
        return out


def _caller(
    name: str,
    uses: str,
    repository: str,
    policy: ModelRoutingScanPolicy,
    sources: Mapping[tuple[str, str], str],
    file_scenarios: _FileScenarios,
    visiting: set[tuple[str, str]],
    build: _Make,
) -> ModelRoutingScenario:
    match = _re(policy.uses_pattern).match(uses.strip())
    callee = (match.group("repo") or repository, match.group("path")) if match else None
    if callee is None or callee not in sources:
        return build(name, EnumRoutingSelection.UNREAD_CALLEE, uses)
    if callee in visiting:
        return build(name, EnumRoutingSelection.CALLER, f"{uses} (cycle)")
    below = file_scenarios(*callee)
    scenario: ModelRoutingScenario = build(name, EnumRoutingSelection.CALLER, uses)
    if any(s.resolution is EnumRoutingResolution.UNRESOLVED for s in below):
        return scenario
    via = (
        EnumRoutingVia.ROUTE
        if any(s.via is EnumRoutingVia.ROUTE for s in below)
        else EnumRoutingVia.DEFINITION
    )
    return scenario.model_copy(
        update={"resolution": EnumRoutingResolution.HOSTED, "via": via}
    )
