# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures: two example repositories on either backend (in memory, or real git clones)."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_code_graph_effect.adapters.repo_sources import (
    GitRepoSource,
    InMemoryRepoSource,
)
from omnimarket.nodes.node_code_graph_effect.overlay import ModelCodeGraphOverlay
from omnimarket.nodes.node_code_graph_effect.protocols import ProtocolRepoSource

EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_code_graph_effect/overlay.example.yaml"
)
RUN_AT = datetime(2026, 11, 7, 6, 40, tzinfo=UTC)
HOST = "example-host"
REF = "origin/main"

CORE: dict[str, str] = {
    ".github/workflows/ci.yml": "jobs:\n  ci:\n    steps:\n      - run: uv run python -m example_core.ci_only --strict\n",
    "src/example_core/ci_only.py": "def run():\n    return 0\n",
    "pyproject.toml": '[project]\nname = "example-core"\n[project.scripts]\nexample-core = "example_core.cli:main"\n',
    "src/example_core/__init__.py": "",
    "src/example_core/cli.py": "from example_core.models import Thing\n\ndef main():\n    return Thing\n",
    "src/example_core/models.py": "class Thing:\n    pass\n",
    "src/example_core/unused.py": "VALUE = 1\n",
    "src/example_core/helpers.py": "def helper():\n    return 2\n",
    "tests/test_helpers.py": "from example_core.helpers import helper\n\ndef test_helper():\n    assert helper() == 2\n",
}
APP: dict[str, str] = {
    "pyproject.toml": (
        '[project]\nname = "example-app"\n'
        '[project.entry-points."onex.nodes"]\nnode_a = "example_app.nodes.node_a"\n'
    ),
    "src/example_app/__init__.py": "",
    "src/example_app/nodes/__init__.py": "",
    "src/example_app/nodes/node_a/__init__.py": "",
    "src/example_app/nodes/node_a/contract.yaml": (
        "name: node_a\nhandler:\n  module: example_app.nodes.node_a.handler\n  class: Handler\n"
    ),
    "src/example_app/nodes/node_a/handler.py": (
        "from example_core.models import Thing\nfrom .util import shape\n\nclass Handler:\n    thing = Thing\n"
    ),
    "src/example_app/nodes/node_a/util.py": "def shape():\n    return 3\n",
    "src/example_app/orphan.py": "ORPHAN = True\n",
    "src/example_app/dyn.py": "LOADED = True\n",
    "scripts/run.py": 'import importlib\n\nimportlib.import_module("example_app.dyn")\n',
}


def overlay(**changes: object) -> ModelCodeGraphOverlay:
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document.update(changes)
    return ModelCodeGraphOverlay.model_validate(document)


def _git(clone: Path, *arguments: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(clone), *arguments],
        capture_output=True,
        text=True,
        check=True,
        env={
            **scrub_git_location_env(),
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
    )
    return done.stdout.strip()


def commit(clone: Path, files: Mapping[str, str], message: str) -> str:
    """Write ``files`` into ``clone``, commit them, and return the commit."""
    for path, text in files.items():
        target = clone / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", message)
    return _git(clone, "rev-parse", "HEAD")


def make_clone(root: Path, name: str, files: Mapping[str, str]) -> str:
    """A clone at ``root/name`` whose ``origin/main`` names a commit of ``files``."""
    clone = root / name
    clone.mkdir(parents=True)
    _git(clone, "init", "-q", "-b", "main")
    head = commit(clone, files, "example")
    _git(clone, "update-ref", "refs/remotes/origin/main", head)
    return head


def source(
    backend: str, base: Path, app: Mapping[str, str] = APP
) -> ProtocolRepoSource:
    """The two example repositories on the named backend."""
    if backend == "memory":
        return InMemoryRepoSource(
            {
                "example_core": {REF: ("c" * 40, CORE)},
                "example_app": {REF: ("a" * 40, app)},
            }
        )
    make_clone(base, "example_core", CORE)
    make_clone(base, "example_app", app)
    return GitRepoSource(base, max_file_bytes=2_000_000)
