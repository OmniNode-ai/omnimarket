# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GraphQL ``nodes(ids:)`` adapter of ProtocolPrNodeStateReader.

One POST per batch. The token is the caller's (the watcher's own); this module
resolves none and prints none.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from omnimarket.github_api import graphql
from omnimarket.nodes.node_pr_watcher_closure_reconcile_effect.models.model_pr_watcher_closure_reconcile import (
    ModelPrNodeState,
)

NODE_STATES_QUERY: Final[str] = """
query($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on PullRequest { id state mergedAt closedAt }
  }
}
"""

GraphqlCall = Callable[..., dict[str, Any]]


class GithubGraphqlNodeStatesReader:
    def __init__(self, token: str, call: GraphqlCall = graphql) -> None:
        self._token = token
        self._call = call

    def read_states(
        self, node_ids: Sequence[str]
    ) -> Mapping[str, ModelPrNodeState | None]:
        ids = list(node_ids)
        data = self._call(NODE_STATES_QUERY, {"ids": ids}, token=self._token)
        nodes = data.get("nodes")
        if not isinstance(nodes, list) or len(nodes) != len(ids):
            raise ValueError("nodes(ids:) answered a list of the wrong shape")
        out: dict[str, ModelPrNodeState | None] = {}
        for node_id, node in zip(ids, nodes, strict=True):
            if not isinstance(node, dict) or node.get("state") not in (
                "OPEN",
                "MERGED",
                "CLOSED",
            ):
                out[node_id] = None
                continue
            out[node_id] = ModelPrNodeState(
                node_id=node_id,
                state=node["state"],
                merged_at=node.get("mergedAt"),
                closed_at=node.get("closedAt"),
            )
        return out


__all__ = ["NODE_STATES_QUERY", "GithubGraphqlNodeStatesReader"]
