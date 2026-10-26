# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the host a lane brief requires, from task metadata at column zero outside fences (OMN-20513, OMN-17427).

Required host: <pool name>
Host-local inputs: <host>:<path> ... (or paths with Required host / --host)

A brief pins a host only by these lines, a merge-sweep diagnose lane's as every other lane's: the merge-sweep workflow
writes ``Required host:`` itself when the plan pins the lane (a controller-reason diagnosis, which reads the landing
controller's own files). The diagnose title line once counted as such a line, and on 2026-10-22 it held a floor-only
diagnosis on a controller host placement refused four times while two pool hosts had a free slot (OMN-17427).
Conflicting declarations and inputs without a named host fail closed (``ValueError``).

``required`` says the brief itself names the host (OMN-17427): a pin only the caller gave is a preference, and the runner falls
back from it to any admitting host when the pinned host refuses the lane; a host the brief requires is kept.
"""

from __future__ import annotations

import re
import shlex

from ..models.model_brief_prerequisites import (
    ModelBriefPrerequisitesRequest,
    ModelBriefPrerequisitesResult,
)

HOST_RE = r"[A-Za-z0-9][A-Za-z0-9_.-]*"
HOME_ROOTS = (
    "/",
    "~/",
    "$HOME/",
    "${HOME}/",
    "$ONEX_REGISTRY_ROOT/",
    "${ONEX_REGISTRY_ROOT}/",
)


class HandlerBriefPrerequisites:
    def handle(
        self, request: ModelBriefPrerequisitesRequest
    ) -> ModelBriefPrerequisitesResult:
        hosts = {request.pinned_host} if request.pinned_host else set()
        declared = False  # the brief names a host of its own
        paths: list[str] = []
        fence: str | None = None
        for line in request.brief.splitlines():
            if line.strip().startswith(("```", "~~~")):
                marker = line.strip()[:3]
                if fence is None:
                    fence = marker
                elif fence == marker:
                    fence = None
                continue
            if fence is not None:
                continue
            match = re.fullmatch(
                r"(Required host|Host-local inputs):[ \t]*(.*)", line, re.IGNORECASE
            )
            if match is None:
                continue
            value = match.group(2).strip()
            if match.group(1).lower() == "required host":
                if not re.fullmatch(HOST_RE, value):
                    raise ValueError("Required host must name one pool host")
                hosts.add(value)
                declared = True
                continue
            inputs = shlex.split(value)
            if not inputs:
                raise ValueError("Host-local inputs must name at least one path")
            for item in inputs:
                qualified = re.fullmatch(rf"({HOST_RE}):(.*)", item)
                if qualified:
                    hosts.add(qualified.group(1))
                    declared = True
                    item = qualified.group(2)
                if not item.startswith(HOME_ROOTS):
                    raise ValueError(
                        "Host-local inputs need absolute or HOME/ONEX_REGISTRY_ROOT-rooted paths"
                    )
                paths.append(item)
        if len(hosts) > 1:
            raise ValueError(
                "conflicting host prerequisites: " + ",".join(sorted(hosts))
            )
        if paths and not hosts:
            raise ValueError(
                "Host-local inputs require a host name (Required host, host:path or --host)"
            )
        required = declared or bool(paths)
        return ModelBriefPrerequisitesResult(
            next(iter(hosts), None), tuple(paths), required
        )
