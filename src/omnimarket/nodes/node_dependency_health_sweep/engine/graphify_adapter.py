# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Graphify adapter — wraps the graphify CLI and owns its output format.

graphify CLI (package: graphifyy==0.8.2, pinned SHA da4ec1d5e713e9a98358601b23662762bf946e36):
  Usage: graphify <path>  OR  python -m graphify <path>
  Output: writes graphify-out/graph.json (configurable via GRAPHIFY_OUT env var)
  Version flag: graphify --version  (prints "graphify <version>")

When graphify is unavailable (FileNotFoundError) or its subprocess times out,
this adapter falls through to ASTImportScanner automatically. Callers always
receive a ModelImportGraph regardless of which path produced it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_dependency_health_sweep.engine.ast_import_scanner import (
    ASTImportScanner,
)
from omnimarket.nodes.node_dependency_health_sweep.models.model_graph_types import (
    ModelImportGraph,
)

_GRAPHIFY_TIMEOUT_S = 120
_PINNED_COMMIT_SHA = "da4ec1d5e713e9a98358601b23662762bf946e36"


class ModelGraphifyProbeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    available: bool
    version: str
    commit_sha: str


class GraphifyAdapter:
    """Probe graphify availability and produce ModelImportGraph from a source tree."""

    def __init__(self) -> None:
        self._probe_result: ModelGraphifyProbeResult | None = None
        self._scanner = ASTImportScanner()

    def probe(self) -> ModelGraphifyProbeResult:
        """Check whether the graphify CLI is available and record its version."""
        if self._probe_result is not None:
            return self._probe_result

        try:
            proc = subprocess.run(
                ["graphify", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError:
            self._probe_result = ModelGraphifyProbeResult(
                available=False, version="", commit_sha=""
            )
            return self._probe_result
        except subprocess.TimeoutExpired:
            self._probe_result = ModelGraphifyProbeResult(
                available=False, version="", commit_sha=""
            )
            return self._probe_result

        if proc.returncode != 0:
            self._probe_result = ModelGraphifyProbeResult(
                available=False, version="", commit_sha=""
            )
            return self._probe_result

        # Parse "graphify <version>" or bare "<version>" from stdout
        raw = proc.stdout.strip()
        version = raw.split()[-1] if raw else ""
        self._probe_result = ModelGraphifyProbeResult(
            available=True,
            version=version,
            commit_sha=_PINNED_COMMIT_SHA,
        )
        return self._probe_result

    def get_import_graph(self, root: Path) -> ModelImportGraph:
        """Return an import graph for the source tree at root.

        Delegates to graphify subprocess when available; falls back to
        ASTImportScanner when graphify is absent or raises FileNotFoundError.
        Raises RuntimeError on subprocess timeout.
        """
        probe = self.probe()

        if not probe.available:
            return self._scanner.scan(root)

        return self._run_graphify(root)

    def _run_graphify(self, root: Path) -> ModelImportGraph:
        out_dir = root / "graphify-out"
        try:
            proc = subprocess.run(
                ["graphify", str(root)],
                capture_output=True,
                text=True,
                timeout=_GRAPHIFY_TIMEOUT_S,
                cwd=str(root),
            )
        except FileNotFoundError:
            return self._scanner.scan(root)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"graphify subprocess timed out after {_GRAPHIFY_TIMEOUT_S}s on {root}"
            ) from exc

        if proc.returncode != 0 or not (out_dir / "graph.json").exists():
            # Gracefully fall back on unexpected graphify failure
            return self._scanner.scan(root)

        return self._parse_graphify_output(out_dir / "graph.json")

    def _parse_graphify_output(self, graph_json: Path) -> ModelImportGraph:
        """Normalize graphify graph.json into ModelImportGraph.

        graphify graph.json schema (as of 0.8.2):
          {
            "nodes": ["module_a", "module_b", ...],
            "edges": [["module_a", "module_b"], ...],
            "orphans": ["module_c", ...]   (may be absent)
          }
        Field names may vary across versions; we normalise defensively.
        """
        try:
            raw = json.loads(graph_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return self._scanner.scan(graph_json.parent.parent)

        nodes: list[str] = raw.get("nodes", [])
        raw_edges = raw.get("edges", [])
        edges: list[tuple[str, str]] = [
            (str(e[0]), str(e[1])) for e in raw_edges if len(e) >= 2
        ]
        orphans: list[str] = raw.get("orphans", raw.get("orphan_modules", []))

        return ModelImportGraph(nodes=nodes, edges=edges, orphan_modules=orphans)
