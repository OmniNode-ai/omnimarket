# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures of the lookup tests: a fixture repository at several commits (OMN-23648)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from omnimarket.models.model_code_graph_lookup import (
    EnumCodeGraphQuestion,
    EnumLookupFault,
    ModelCodeGraphLookupCommand,
    ModelCodeGraphVersion,
)
from omnimarket.nodes.node_code_graph_effect.adapters.repo_sources import (
    InMemoryRepoSource,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_index import (
    build_version,
)
from omnimarket.nodes.node_code_graph_effect.handlers.graph_compute import GraphRules
from tests.nodes.node_code_graph_effect.code_graph_support import REF, overlay

REPO = "example_core"
CORRELATION = "lookup-1"

MODELS = "src/example_core/models.py"
MIRROR = "src/example_core/mirror.py"
LOOKALIKE = "src/example_core/lookalike.py"
HANDLER = "src/example_core/handler.py"
CLI = "src/example_core/cli.py"
TEST = "tests/test_handler.py"

#: Two models of one shape under different names (Thing, Mirror), and a class named Thing of another shape.
BASE: dict[str, str] = {
    "src/example_core/__init__.py": "",
    MODELS: "from pydantic import BaseModel\n\n\nclass Thing(BaseModel):\n    name: str\n    count: int\n",
    MIRROR: "class Mirror:\n    count: int\n    name: str\n",
    LOOKALIKE: "class Thing:\n    size: float\n",
    HANDLER: "from example_core.models import Thing\n\n\ndef handle(request):\n    return Thing\n",
    CLI: "from example_core.handler import handle\n\n\ndef main():\n    return handle\n",
    TEST: "from example_core.handler import handle\n\n\ndef test_handle():\n    assert handle\n",
    "src/example_core/nodes/node_alpha_effect/contract.yaml": "name: node_alpha_effect\ndescriptor:\n  node_archetype: effect\n",
    "src/example_core/nodes/node_beta_effect/contract.yaml": "name: node_beta_effect\ndescriptor:\n  node_archetype: effect\n",
    "src/example_core/nodes/node_gamma_compute/contract.yaml": "name: node_gamma_compute\nnode_type: compute\n",
}

#: A later commit adds one symbol, Added, in its own file.
ADDS = {"src/example_core/added.py": "def added_symbol():\n    return 1\n"}


def rules() -> GraphRules:
    return GraphRules(roots=overlay().graph_roots)


def version(
    commit: str,
    files: Mapping[str, str],
    committed_at: datetime | None = None,
) -> ModelCodeGraphVersion:
    graph_rules = rules()
    snapshot = InMemoryRepoSource({REPO: {REF: (commit, files)}}).snapshot(
        REPO, REF, graph_rules.wanted
    )
    return build_version(snapshot, graph_rules, committed_at=committed_at)


def at(day: int) -> datetime:
    return datetime(2026, 11, day, 12, 0, tzinfo=UTC)


def ask(
    question: EnumCodeGraphQuestion,
    *,
    repo: str = REPO,
    symbol: str | None = None,
    path: str | None = None,
    paths: tuple[str, ...] = (),
    archetype: str | None = None,
    hint: str | None = None,
    as_of: str | None = None,
    fault: EnumLookupFault = EnumLookupFault.NONE,
) -> ModelCodeGraphLookupCommand:
    return ModelCodeGraphLookupCommand(
        correlation_id=CORRELATION,
        question=question,
        repo=repo,
        symbol=symbol,
        path=path,
        paths=paths,
        archetype=archetype,
        hint=hint,
        as_of=as_of,
        fault=fault,
    )
