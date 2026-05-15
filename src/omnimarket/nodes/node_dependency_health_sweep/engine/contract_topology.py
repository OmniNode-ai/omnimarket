# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract topology parser for node_dependency_health_sweep.

Walks search_roots for contract.yaml files, builds the pub/sub topic graph,
detects orphan topics (published but never subscribed with no external consumer
declaration and no allowlist entry), and finds hardcoded topic literals in
source files that are not declared in any contract.

Topic authority: contract.yaml is the single source of truth. TopicBase enum
is used as a best-effort cross-validation signal only (guarded with try/except).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_dependency_health_sweep.models.model_graph_types import (
    ModelTopologyGraph,
)

logger = logging.getLogger(__name__)

# Matches onex.cmd.* and onex.evt.* topic strings anywhere in source text.
_TOPIC_LITERAL_RE = re.compile(
    r"onex\.(cmd|evt)\.[a-z0-9_\-]+(?:\.[a-z0-9][a-z0-9\-]*)+\.v[0-9]+"
)

# Source file extensions to scan for hardcoded topic literals.
_SOURCE_EXTENSIONS = {".py", ".ts", ".tsx", ".yaml", ".yml"}

# Files whose topic-looking strings are explicitly produced by the contract
# system (these are the contracts themselves — exclude to avoid self-reporting).
_SKIP_FILENAMES = {"contract.yaml", "dep_health_allowlist.yaml"}


class ContractTopologyParser:
    """Parse contract.yaml files under search_roots and build a topology graph."""

    def parse(self, search_roots: list[Path]) -> ModelTopologyGraph:
        """Walk search_roots, collect contract pub/sub topics, return topology graph.

        Args:
            search_roots: List of root directories to search recursively.

        Returns:
            ModelTopologyGraph with nodes, pub/sub edges, orphan topics,
            and undeclared topics.
        """
        all_contracts: list[dict[str, Any]] = []
        all_known_topics: set[str] = set()
        externally_consumed: set[str] = set()
        allowlisted: set[str] = set()

        for root in search_roots:
            # Load allowlist if present at root level
            allowlist_path = root / "dep_health_allowlist.yaml"
            if allowlist_path.is_file():
                self._load_allowlist(allowlist_path, allowlisted)

            # Discover and parse all contract.yaml files
            for contract_path in sorted(root.rglob("contract.yaml")):
                data = self._load_yaml(contract_path)
                if data is None:
                    continue
                all_contracts.append(data)

                # Collect externally_consumed_topics declared at contract level
                for topic in data.get("externally_consumed_topics", []):
                    externally_consumed.add(str(topic))

                # Collect all declared topics (pub + sub) into the known set
                event_bus = data.get("event_bus") or {}
                for topic in event_bus.get("publish_topics", []) or []:
                    all_known_topics.add(str(topic))
                for topic in event_bus.get("subscribe_topics", []) or []:
                    all_known_topics.add(str(topic))

        # Build node list, pub/sub edge lists from parsed contracts
        nodes: list[str] = []
        pub_edges: list[tuple[str, str, str]] = []
        sub_edges: list[tuple[str, str, str]] = []

        published: set[str] = set()
        subscribed: set[str] = set()

        for data in all_contracts:
            node_name: str = str(data.get("name", ""))
            if node_name and node_name not in nodes:
                nodes.append(node_name)

            event_bus = data.get("event_bus") or {}

            for topic in event_bus.get("publish_topics", []) or []:
                topic_str = str(topic)
                pub_edges.append((node_name, topic_str, "pub"))
                published.add(topic_str)

            for topic in event_bus.get("subscribe_topics", []) or []:
                topic_str = str(topic)
                sub_edges.append((node_name, topic_str, "sub"))
                subscribed.add(topic_str)

        # Orphan: published but no subscriber, not externally consumed, not allowlisted
        orphan_topics = [
            t
            for t in published
            if t not in subscribed
            and t not in externally_consumed
            and t not in allowlisted
        ]

        # Scan source files for hardcoded topic literals not in any contract
        undeclared_topics = self._find_undeclared_topics(search_roots, all_known_topics)

        # Best-effort TopicBase cross-validation (INFO only — does not affect graph)
        self._cross_check_topic_base(all_known_topics)

        return ModelTopologyGraph(
            nodes=sorted(nodes),
            pub_edges=pub_edges,
            sub_edges=sub_edges,
            orphan_topics=sorted(orphan_topics),
            undeclared_topics=sorted(undeclared_topics),
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_yaml(self, path: Path) -> dict[str, Any] | None:
        try:
            with path.open() as fh:
                data = yaml.safe_load(fh)
            if not isinstance(data, dict):
                return None
            return data
        except Exception:
            logger.warning("Failed to parse %s — skipping", path)
            return None

    def _load_allowlist(self, path: Path, allowlisted: set[str]) -> None:
        data = self._load_yaml(path)
        if data is None:
            return
        for entry in data.get("allowlist", []) or []:
            if isinstance(entry, dict) and "topic" in entry:
                allowlisted.add(str(entry["topic"]))

    def _find_undeclared_topics(
        self, search_roots: list[Path], known_topics: set[str]
    ) -> set[str]:
        """Scan source files for topic string literals not declared in any contract."""
        undeclared: set[str] = set()
        for root in search_roots:
            for ext in _SOURCE_EXTENSIONS:
                for src_file in root.rglob(f"*{ext}"):
                    if src_file.name in _SKIP_FILENAMES:
                        continue
                    try:
                        text = src_file.read_text(errors="replace")
                    except Exception:
                        continue
                    for match in _TOPIC_LITERAL_RE.finditer(text):
                        topic = match.group(0)
                        if topic not in known_topics:
                            undeclared.add(topic)
        return undeclared

    def _cross_check_topic_base(self, known_topics: set[str]) -> None:
        """Cross-validate contract topics against TopicBase enum (INFO only)."""
        try:
            from omniclaude.hooks.topics import TopicBase

            topic_base_values = {e.value for e in TopicBase}
            for topic in known_topics:
                if topic not in topic_base_values:
                    logger.info(
                        "Contract topic %r not found in TopicBase enum (INFO only)",
                        topic,
                    )
        except ImportError:
            pass
