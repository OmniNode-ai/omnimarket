# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Move merged or closed pull requests out of the PR watcher's open set.

The watcher's own closure read only looks at OPEN records that are in neither
the search hits nor the REST open lists, at most a fixed number per tick over
the sorted key list, with no position kept between ticks. A record the search
index still lists, a record that sorts after the cap, or a merge that falls in
a tick gap longer than the search overlap can then stay OPEN indefinitely.

This handler reads every OPEN record that is not in the REST open lists,
whatever the search index says:

* oldest read first: each record's last read time is kept in a cursor file
  beside the state file, and a never-read record sorts before every read one,
  so no record is starved by key order;
* bounded: at most ``batch_size * max_batches`` records and ``max_batches``
  GraphQL calls per tick (one ``nodes(ids:)`` call per batch), so a full pass
  over N candidates takes at most ``ceil(N / (batch_size * max_batches))``
  ticks;
* no time window: the read does not depend on ``updated:>=`` or on the gap
  between ticks, and the cursor survives a restart;
* the flip is written under the watcher's ``state.lock`` (an exclusive
  ``flock``), re-reading the state inside the lock and replacing the file
  atomically, and only for a record that is still OPEN with the same node id.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from uuid import NAMESPACE_URL, uuid5

import yaml

from omnimarket import github_api
from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.inference import secret_store_resolver
from omnimarket.nodes.node_pr_watcher_closure_reconcile_effect.models.model_pr_watcher_closure_reconcile import (
    ModelClosureFlip,
    ModelPrNodeState,
    ModelPrWatcherClosureObservation,
    ModelPrWatcherClosureOverlay,
    ModelPrWatcherClosureReconcileRequest,
    ModelPrWatcherClosureReconcileResult,
    ModelPrWatcherClosureTickRequest,
    ModelPrWatcherClosureTickResult,
)
from omnimarket.nodes.node_pr_watcher_closure_reconcile_effect.protocols.github_graphql_node_states_reader import (
    GithubGraphqlNodeStatesReader,
)
from omnimarket.nodes.node_pr_watcher_closure_reconcile_effect.protocols.protocol_pr_node_state_reader import (
    ProtocolPrNodeStateReader,
)

if TYPE_CHECKING:
    from omnibase_core.container import ModelONEXContainer

CONTRACT_PATH: Final[Path] = Path(__file__).resolve().parent.parent / "contract.yaml"


def load_policy(contract_path: Path = CONTRACT_PATH) -> dict[str, Any]:
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    policy = contract["policy"]
    if not isinstance(policy, dict):
        raise ValueError("contract policy is not a mapping")
    return policy


class HandlerPrWatcherClosureReconcile:
    """The reader is resolved from the container (ProtocolPrNodeStateReader)."""

    def __init__(
        self,
        container: ModelONEXContainer | None = None,
        *,
        reader: ProtocolPrNodeStateReader | None = None,
    ) -> None:
        self._container = container
        self._injected_reader = reader
        self._policy = load_policy()

    def _reader(self) -> ProtocolPrNodeStateReader:
        if self._injected_reader is not None:
            return self._injected_reader
        if self._container is None:
            raise ValueError("ProtocolPrNodeStateReader is not bound")
        key: Any = ProtocolPrNodeStateReader
        reader: ProtocolPrNodeStateReader = self._container.get_service(key)
        return reader

    def handle(
        self, request: ModelPrWatcherClosureReconcileRequest
    ) -> ModelPrWatcherClosureReconcileResult:
        state_path = Path(request.state_path)
        lock_path = state_path.with_name(str(self._policy["lock_name"]))
        cursor_path = state_path.with_name(str(self._policy["cursor_name"]))
        batch_size = request.batch_size or int(self._policy["batch_size"])
        max_batches = request.max_batches or int(self._policy["max_batches_per_tick"])
        per_tick = batch_size * max_batches

        state = _read_json(state_path)
        cursor = _read_json(cursor_path)
        last_read: dict[str, str] = {
            k: v
            for k, v in _dict(cursor.get("last_read")).items()
            if isinstance(v, str)
        }
        rest_open = set(request.rest_open_keys)

        candidates: dict[str, str] = {}
        no_node_id: list[str] = []
        for key, record in _dict(state.get("prs")).items():
            facts = _dict(_dict(record).get("facts"))
            if facts.get("state") != "OPEN" or key in rest_open:
                continue
            node_id = facts.get("node_id")
            if isinstance(node_id, str) and node_id:
                candidates[key] = node_id
            else:
                no_node_id.append(key)

        # Oldest read first; "" (never read) sorts before every timestamp.
        order = sorted(candidates, key=lambda k: (last_read.get(k, ""), k))
        chosen = order[:per_tick]

        answers: dict[str, ModelPrNodeState | None] = {}
        read_keys: list[str] = []
        calls = 0
        read_failed = False
        for start in range(0, len(chosen), batch_size):
            batch = chosen[start : start + batch_size]
            calls += 1
            try:
                got = self._reader().read_states([candidates[k] for k in batch])
            except Exception:
                # A failed batch flips nothing and keeps its records at the
                # head of the order for the next tick.
                read_failed = True
                break
            for key in batch:
                answers[key] = got.get(candidates[key])
                read_keys.append(key)

        flips: list[ModelClosureFlip] = []
        unanswered: list[str] = []
        for key in read_keys:
            answer = answers[key]
            if answer is None:
                unanswered.append(candidates[key])
            elif answer.state != "OPEN":
                flips.append(
                    ModelClosureFlip(
                        key=key,
                        state=answer.state,
                        merged_at=answer.merged_at,
                        closed_at=answer.closed_at,
                    )
                )

        applied: list[ModelClosureFlip] = []
        with _locked(lock_path):
            if flips:
                fresh = _read_json(state_path)
                prs = _dict(fresh.get("prs"))
                for flip in flips:
                    record = _dict(prs.get(flip.key))
                    facts = _dict(record.get("facts"))
                    if (
                        facts.get("state") != "OPEN"
                        or facts.get("node_id") != candidates[flip.key]
                    ):
                        continue
                    facts["state"] = flip.state
                    facts["armed"] = False
                    if flip.merged_at is not None:
                        facts["merged_at"] = flip.merged_at
                    if flip.closed_at is not None:
                        facts["closed_at"] = flip.closed_at
                    # Snapshot readers also group by class. A terminal fact
                    # must not retain its green/queued eligibility, and the
                    # watcher must emit the corrected state on its next tick.
                    previous = str(record.get("cls", "unread"))
                    record["cls"] = flip.state.lower()
                    record["reason"] = (
                        f"{flip.state.lower()} {flip.merged_at or flip.closed_at or ''}".strip()
                        + f"; last open class {previous}"
                    )
                    record["queued"] = False
                    record["blocked"] = False
                    record["bus_closed_emitted"] = False
                    applied.append(flip)
                if applied:
                    _write_json(state_path, fresh)
            for key in read_keys:
                last_read[key] = request.now
            done = {flip.key for flip in applied}
            kept = {
                k: v for k, v in last_read.items() if k in candidates and k not in done
            }
            _write_json(cursor_path, {"last_read": kept})

        return ModelPrWatcherClosureReconcileResult(
            candidates=len(candidates),
            read_keys=tuple(read_keys),
            calls=calls,
            flipped=tuple(applied),
            no_node_id=tuple(sorted(no_node_id)),
            unanswered=tuple(unanswered),
            ticks_to_full_pass=math.ceil(len(candidates) / per_tick),
            read_failed=read_failed,
        )


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return _dict(loaded)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    with lock_path.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = ["CONTRACT_PATH", "HandlerPrWatcherClosureReconcile", "load_policy"]


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
                terminal_failure_cause="read-failed" if result.read_failed else None,
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
