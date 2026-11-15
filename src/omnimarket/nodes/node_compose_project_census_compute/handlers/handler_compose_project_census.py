# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-project compose census: clone placement, file match and image match."""

import re

from ..models.model_compose_project_census import (
    ComposeCensusHostUndeclaredError,
    EnumComposeCensusFinding,
    ModelComposeCensusFinding,
    ModelComposeCensusProject,
    ModelComposeCensusRequest,
    ModelComposeCensusVerdict,
)


def _expand(path: str, home_dir: str) -> str:
    """A leading ``~/`` is the host login's home; with no home given it matches nothing."""
    if path.startswith("~/"):
        return f"{home_dir}/{path[2:]}" if home_dir else "/.unresolved-home/" + path[2:]
    return path


def _under(path_parts: list[str], root: str) -> list[str] | None:
    """The path components below ``root``, or None when the path is not under it (whole components only)."""
    root_parts = [p for p in root.split("/") if p]
    if len(root_parts) > len(path_parts) or path_parts[: len(root_parts)] != root_parts:
        return None
    return path_parts[len(root_parts) :]


def _under_any(path: str, roots: tuple[str, ...], home_dir: str) -> bool:
    parts = [p for p in path.split("/") if p]
    return any(_under(parts, _expand(root, home_dir)) is not None for root in roots)


def _is_inside_clone(
    working_dir: str,
    clone_roots: tuple[str, ...],
    worktree_segments: tuple[str, ...],
    deployed_roots: tuple[str, ...],
    home_dir: str,
) -> bool:
    parts = [p for p in working_dir.split("/") if p]
    for root in deployed_roots:
        if _under(parts, _expand(root, home_dir)) is not None:
            return True
    for root in clone_roots:
        tail = _under(parts, _expand(root, home_dir))
        if tail is not None and not any(seg in worktree_segments for seg in tail):
            return True
    return False


def _mixed_working_dirs(
    request: ModelComposeCensusRequest,
) -> list[ModelComposeCensusFinding]:
    """One project run from more than one directory: the minority containers are the stragglers.

    The majority directory is the one most containers use; a tie goes to the greatest path, so the
    answer does not depend on input order.
    """
    by_project: dict[str, dict[str, list[str]]] = {}
    for container in request.containers:
        project = container.labels.get("com.docker.compose.project", "")
        working_dir = container.labels.get("com.docker.compose.project.working_dir", "")
        if project and working_dir:
            by_project.setdefault(project, {}).setdefault(working_dir, []).append(
                container.name
            )
    findings: list[ModelComposeCensusFinding] = []
    for project, dirs in by_project.items():
        if len(dirs) < 2:
            continue
        majority = max(dirs, key=lambda d: (len(dirs[d]), d))
        for working_dir, names in dirs.items():
            if working_dir == majority:
                continue
            findings.extend(
                ModelComposeCensusFinding(
                    container=name,
                    kind=EnumComposeCensusFinding.PROJECT_MIXED_WORKING_DIRS,
                    detail=(
                        f"project {project!r} runs from {working_dir!r} here and from "
                        f"{majority!r} for most of its containers"
                    ),
                )
                for name in names
            )
    return findings


class HandlerComposeProjectCensus:
    def handle(self, request: ModelComposeCensusRequest) -> ModelComposeCensusVerdict:
        host_names = {h.name for h in request.policy.hosts}
        if request.host not in host_names:
            raise ComposeCensusHostUndeclaredError(
                f"host {request.host!r} is not a host of the policy"
            )

        host = next(h for h in request.policy.hosts if h.name == request.host)
        clone_roots = host.clone_roots
        worktree_segments = request.policy.worktree_segments

        project_by_host: dict[str, ModelComposeCensusProject] = {}
        for p in request.policy.projects:
            if request.host in p.hosts:
                project_by_host[p.project] = p

        findings: list[ModelComposeCensusFinding] = []

        for container in request.containers:
            labels = container.labels
            project = labels.get("com.docker.compose.project", "")

            if project == "":
                matched = any(
                    request.host in s.hosts and container.name.startswith(s.name_prefix)
                    for s in request.policy.standalone
                )
                transient = any(
                    re.fullmatch(pattern, container.name)
                    for pattern in request.policy.transient_name_patterns
                )
                if not matched and not transient:
                    findings.append(
                        ModelComposeCensusFinding(
                            container=container.name,
                            kind=EnumComposeCensusFinding.CONTAINER_UNDECLARED,
                            detail=f"container {container.name!r} has no compose project label and matches no standalone prefix",
                        )
                    )
                continue

            working_dir = labels.get("com.docker.compose.project.working_dir", "")
            if not _is_inside_clone(
                working_dir,
                clone_roots,
                worktree_segments,
                request.policy.deployed_roots,
                request.home_dir,
            ):
                findings.append(
                    ModelComposeCensusFinding(
                        container=container.name,
                        kind=EnumComposeCensusFinding.COMPOSE_DIR_OUTSIDE_CLONE,
                        detail=f"compose directory {working_dir!r} is not inside a clone",
                    )
                )

            if working_dir in request.absent_paths:
                findings.append(
                    ModelComposeCensusFinding(
                        container=container.name,
                        kind=EnumComposeCensusFinding.COMPOSE_FILE_MISSING,
                        detail=f"compose directory {working_dir!r} is missing on the host",
                    )
                )

            declared = project_by_host.get(project)
            if declared is None:
                findings.append(
                    ModelComposeCensusFinding(
                        container=container.name,
                        kind=EnumComposeCensusFinding.PROJECT_UNDECLARED,
                        detail=f"project {project!r} is not declared for host {request.host!r}",
                    )
                )
                continue

            config_files = labels.get("com.docker.compose.project.config_files", "")
            actual_files = [f.strip() for f in config_files.split(",") if f.strip()]
            declared_paths = [
                f"{_expand(root, request.home_dir)}/{declared.compose_file}"
                for root in clone_roots
            ]
            # A tree a declared installer wrote is that installer's output: its placement and its single version
            # are checked, its path is not compared to the clone's.
            installed = _under_any(
                working_dir, request.policy.deployed_roots, request.home_dir
            )
            if not installed and not any(f in declared_paths for f in actual_files):
                findings.append(
                    ModelComposeCensusFinding(
                        container=container.name,
                        kind=EnumComposeCensusFinding.COMPOSE_FILE_DIFFERS,
                        detail=f"declared {declared_paths!r} not among actual {actual_files!r}",
                    )
                )

            service = labels.get("com.docker.compose.service", "")
            for img in declared.images:
                if img.service == service:
                    if container.image != img.image:
                        findings.append(
                            ModelComposeCensusFinding(
                                container=container.name,
                                kind=EnumComposeCensusFinding.VERSION_MISMATCH,
                                detail=f"declared {img.image!r} but running {container.image!r}",
                            )
                        )
                    break

        findings.extend(_mixed_working_dirs(request))
        findings.sort(key=lambda f: (f.container, f.kind.value))
        return ModelComposeCensusVerdict(
            host=request.host,
            findings=tuple(findings),
            clean=len(findings) == 0,
        )
