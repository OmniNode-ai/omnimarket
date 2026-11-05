# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bus boundary for the host-bound closure reconciler."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from omnimarket import github_api
from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.inference import secret_store_resolver
from omnimarket.nodes.node_pr_watcher_closure_reconcile_effect.handlers.handler_pr_watcher_closure_reconcile import (
    HandlerPrWatcherClosureReconcile,
    _locked,
    _read_json,
    _write_json,
    load_policy,
)
from omnimarket.nodes.node_pr_watcher_closure_reconcile_effect.models.model_pr_watcher_closure_reconcile import (
    ModelPrWatcherClosureObservation,
    ModelPrWatcherClosureOverlay,
    ModelPrWatcherClosureReconcileRequest,
    ModelPrWatcherClosureTickRequest,
    ModelPrWatcherClosureTickResult,
)
from omnimarket.nodes.node_pr_watcher_closure_reconcile_effect.protocols.github_graphql_node_states_reader import (
    GithubGraphqlNodeStatesReader,
)

NODE_NAME = "node_pr_watcher_closure_reconcile_effect"


def utc_now() -> datetime:
    return datetime.now(UTC)


class HandlerPrWatcherClosureTick:
    """One bounded pass per snapshot, including across consumer restarts.

    The additional dispatch lock serializes duplicate observations without
    recursively taking the watcher's state lock. Paths and identity belong
    to the deployment overlay of the host that owns the snapshot.
    """

    def handle(
        self, request: ModelPrWatcherClosureTickRequest
    ) -> ModelPrWatcherClosureTickResult:
        overlay = load_structured_overlay(NODE_NAME, ModelPrWatcherClosureOverlay)
        if not overlay.state_path.is_absolute():
            raise ValueError("closure overlay state_path must be absolute")
        policy = load_policy()
        state_path = overlay.state_path
        lock_path = state_path.with_name(str(policy["dispatch_lock_name"]))
        receipt_path = state_path.with_name(str(policy["dispatch_receipt_name"]))
        with _locked(lock_path):
            if not state_path.is_file():
                raise ValueError("closure snapshot is missing")
            state = _read_json(state_path)
            stamp = state.get("last_tick")
            if (
                state.get("schema") != 1
                or not isinstance(stamp, str)
                or not isinstance(state.get("prs"), dict)
            ):
                raise ValueError("closure snapshot has no schema-1 tick")
            source_time = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if source_time.tzinfo is None:
                raise ValueError("closure snapshot clock must carry a timezone")
            age = (utc_now() - source_time).total_seconds()
            if not 0 <= age <= int(policy["max_snapshot_age_seconds"]):
                return ModelPrWatcherClosureTickResult(
                    correlation_id=request.correlation_id,
                    source_tick=stamp,
                    status="stale-state",
                )
            previous = _read_json(receipt_path).get("source_tick")
            if isinstance(previous, str) and source_time <= datetime.fromisoformat(
                previous.replace("Z", "+00:00")
            ):
                return ModelPrWatcherClosureTickResult(
                    correlation_id=request.correlation_id,
                    source_tick=stamp,
                    status="already-reconciled",
                )
            token = secret_store_resolver.resolve_api_key_loop_safe(
                overlay.github_token_secret_ref
            )
            if token is None:
                raise ValueError(
                    "closure read identity is not bound in the secret store"
                )
            reader = GithubGraphqlNodeStatesReader(
                token.get_secret_value(), call=github_api.graphql
            )
            # Reserve before reading: a failed batch or consumer crash cannot
            # multiply the API budget through replay. The next snapshot retries
            # the oldest still-unread candidates.
            _write_json(receipt_path, {"source_tick": stamp, "status": "started"})
            result = HandlerPrWatcherClosureReconcile(reader=reader).handle(
                ModelPrWatcherClosureReconcileRequest(
                    state_path=str(state_path),
                    now=stamp,
                )
            )
            status = "read-failed" if result.read_failed else "reconciled"
            terminal = ModelPrWatcherClosureTickResult(
                correlation_id=request.correlation_id,
                source_tick=stamp,
                status=status,
                reconciliation=result,
            )
            _write_json(receipt_path, terminal.model_dump(mode="json"))
            return terminal


class HandlerPrWatcherClosureObservation:
    """Use the existing producer's observation as the trigger."""

    def handle(
        self, request: ModelPrWatcherClosureObservation
    ) -> ModelPrWatcherClosureTickResult:
        return HandlerPrWatcherClosureTick().handle(
            ModelPrWatcherClosureTickRequest(
                correlation_id=uuid5(NAMESPACE_URL, request.observed_at.isoformat()),
            )
        )
