# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bind one node package into ``sys.modules`` without running its package ``__init__`` (OMN-24321).

A node's package ``__init__`` may bind a handler that needs the full runtime (the landing decision node's binds its
J8 join, which needs omnibase_core and omnibase_infra), while the handler a caller wants needs only pydantic. The
landing controller's decision environment holds pydantic and pyyaml and nothing else, so its bridge binds the
package here: the package is registered with a namespace spec (``importlib.resources.files`` still reads the
node's own ``contract.yaml``) and its submodules import from the node's directory; the ``__init__`` never runs.
A node name that is not a plain node directory name, or a directory that is absent, is refused with a typed
reason and binds nothing.
"""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path

from pydantic import ValidationError

from omnimarket.nodes.node_package_bind_effect.models.model_package_bind import (
    ModelPackageBindRequest,
    ModelPackageBindResult,
)

NODE_NAME_RE = re.compile(r"^node_[a-z0-9_]+$")


class HandlerPackageBind:
    """``handle(ModelPackageBindRequest) -> ModelPackageBindResult``."""

    def handle(self, request: ModelPackageBindRequest) -> ModelPackageBindResult:
        node = request.node
        if not NODE_NAME_RE.match(node):
            return _refused(request, f"bad-node-name:{node}")
        src = Path(request.src)
        path = src / "omnimarket" / "nodes" / node
        if not path.is_dir():
            return _refused(request, f"no-package-dir:{path}")
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
        nodes = importlib.import_module("omnimarket.nodes")
        name = f"omnimarket.nodes.{node}"
        # A namespace-package spec, so importlib.resources.files(name) reads the node's own contract.yaml (a bare module has no spec).
        spec = importlib.machinery.ModuleSpec(
            name,
            importlib.machinery.NamespaceLoader(name, [str(path)], _find_spec),
            is_package=True,
        )
        spec.submodule_search_locations = [str(path)]
        package = importlib.util.module_from_spec(spec)
        sys.modules[name] = package
        setattr(nodes, node, package)
        return ModelPackageBindResult(
            correlation_id=request.correlation_id,
            outcome="bound",
            package=name,
            path=str(path),
        )


def _find_spec(fullname: str, paths: tuple[str, ...]) -> importlib.machinery.ModuleSpec:
    """The namespace path finder: the spec a recomputed namespace path resolves to."""
    found = importlib.machinery.PathFinder.find_spec(fullname, list(paths))
    if found is None:
        raise ModuleNotFoundError(fullname)
    return found


def _refused(request: ModelPackageBindRequest, reason: str) -> ModelPackageBindResult:
    return ModelPackageBindResult(
        correlation_id=request.correlation_id, outcome="refused", reason=reason
    )


def answer_command(
    payload: bytes,
    handler: HandlerPackageBind,
    topics: Mapping[str, str],
) -> list[tuple[str, bytes]]:
    """Answer one command: the run-completed event of a bind, or one run-failed event for a refusal or a bad payload."""
    try:
        request = ModelPackageBindRequest.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {str(error["loc"][0]) for error in exc.errors() if error["loc"]}
        )
        body: dict[str, str] = {
            "correlation_id": "unknown",
            "failure": f"bad-payload:{','.join(fields)}",
        }
        return [
            (
                topics["package_bind_run_failed"],
                json.dumps(body, sort_keys=True).encode("utf-8"),
            )
        ]
    result = handler.handle(request)
    if result.outcome == "refused":
        failed = {"correlation_id": result.correlation_id, "failure": result.reason}
        return [
            (
                topics["package_bind_run_failed"],
                json.dumps(failed, sort_keys=True).encode("utf-8"),
            )
        ]
    completed = {
        "correlation_id": result.correlation_id,
        "package": result.package,
        "path": result.path,
        "ok": True,
    }
    return [
        (
            topics["package_bind_run_completed"],
            json.dumps(completed, sort_keys=True).encode("utf-8"),
        )
    ]


__all__ = ["HandlerPackageBind", "answer_command"]
