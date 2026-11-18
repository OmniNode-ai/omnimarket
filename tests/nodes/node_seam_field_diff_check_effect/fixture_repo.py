# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A tiny synthetic package and a REAL temporary git repository for the seam field diff check (OMN-24121).

The package holds one producer node and one consumer node on one topic. Every test names its
own package so the in-process model import of one test never serves another's. Commits are real
git commits, so the base revision is read the way the check reads it in CI.
"""

from __future__ import annotations

import itertools
import os
import subprocess
from pathlib import Path

from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

TOPIC = "seamfixture.evt.alpha-completed"
_COUNTER = itertools.count(1)

PRODUCER_FIELDS = "    correlation_id: str\n    count: int\n"
MATCHING_CONSUMER_FIELDS = "    correlation_id: str\n"
MISSING_FIELD_CONSUMER_FIELDS = "    correlation_id: str\n    label: str\n"


def new_package_name() -> str:
    return f"seamfix{next(_COUNTER)}"


def git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    return completed.stdout


def config(package: str, *, floor: int = 1) -> dict[str, object]:
    return {
        "repo_packages": {package: f"src/{package}"},
        "contract_packages": [package],
        "envelope_models": [],
        "nested_depth_limit": 3,
        "min_pairs_compared": floor,
        "base_load_timeout_s": 120,
    }


def write_package(
    repo: Path,
    package: str,
    *,
    consumer_fields: str = MATCHING_CONSUMER_FIELDS,
    consumer_extra: str = "forbid",
    consumer_prelude: str = "",
    consumer_name: str = "ModelBetaRequest",
) -> None:
    """(Re)write the package: alpha publishes ModelAlphaEvent, beta validates its request."""
    root = repo / "src" / package
    for directory in (root, root / "models", root / "handlers", root / "nodes"):
        directory.mkdir(parents=True, exist_ok=True)
    for init in (root, root / "models", root / "handlers"):
        (init / "__init__.py").write_text("")
    (root / "models" / "alpha.py").write_text(
        "from pydantic import BaseModel\n\n\n"
        f"class ModelAlphaEvent(BaseModel):\n{PRODUCER_FIELDS}"
    )
    (root / "models" / "beta.py").write_text(
        "from pydantic import BaseModel, ConfigDict, model_validator\n\n\n"
        f"class {consumer_name}(BaseModel):\n"
        f"    model_config = ConfigDict(extra={consumer_extra!r})\n"
        f"{consumer_fields}{consumer_prelude}"
    )
    (root / "handlers" / "handler_beta.py").write_text(
        f"from {package}.models.beta import {consumer_name}\n\n\n"
        "class HandlerBeta:\n"
        f"    def handle(self, request: {consumer_name}) -> {consumer_name}:\n"
        "        return request\n"
    )
    (root / "handlers" / "handler_alpha.py").write_text(
        f"from {package}.models.alpha import ModelAlphaEvent\n\n\n"
        "class HandlerAlpha:\n"
        "    def handle(self, request: ModelAlphaEvent) -> ModelAlphaEvent:\n"
        "        return request\n"
    )
    for node, key, handler, model_field in (
        ("node_alpha", "publish_topics", "HandlerAlpha", "output_model"),
        ("node_beta", "subscribe_topics", "HandlerBeta", ""),
    ):
        directory = root / "nodes" / node
        directory.mkdir(parents=True, exist_ok=True)
        module = f"{package}.handlers.handler_{handler[7:].lower()}"
        lines = [
            f"name: {node}",
            "event_bus:",
            f"  {key}:",
            f"    - {TOPIC}",
            "handler_routing:",
            "  routing_strategy: operation_match",
            "  handlers:",
            f"    - operation: run_{node}",
            "      handler:",
            f"        name: {handler}",
            f"        module: {module}",
        ]
        if model_field:
            lines += [
                "      output_model:",
                "        name: ModelAlphaEvent",
                f"        module: {package}.models.alpha",
            ]
        (directory / "contract.yaml").write_text("\n".join(lines) + "\n")


def new_repo(tmp_path: Path, package: str, **kwargs: str) -> Path:
    """A git repository whose first commit holds the package as the base revision."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "seam@example.invalid")
    git(repo, "config", "user.name", "seam fixture")
    write_package(repo, package, **kwargs)
    commit(repo, "base")
    return repo


def commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
