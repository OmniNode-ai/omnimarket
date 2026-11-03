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
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import yaml

from omnimarket.nodes.node_pr_watcher_closure_reconcile_effect.models.model_pr_watcher_closure_reconcile import (
    ModelClosureFlip,
    ModelPrNodeState,
    ModelPrWatcherClosureReconcileRequest,
    ModelPrWatcherClosureReconcileResult,
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

    def __init__(self, container: ModelONEXContainer) -> None:
        self._container = container
        self._policy = load_policy()

    def _reader(self) -> ProtocolPrNodeStateReader:
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
        for start in range(0, len(chosen), batch_size):
            batch = chosen[start : start + batch_size]
            calls += 1
            try:
                got = self._reader().read_states([candidates[k] for k in batch])
            except Exception:
                # A failed batch flips nothing and keeps its records at the
                # head of the order for the next tick.
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
                    facts = _dict(_dict(prs.get(flip.key)).get("facts"))
                    if (
                        facts.get("state") != "OPEN"
                        or facts.get("node_id") != candidates[flip.key]
                    ):
                        continue
                    facts["state"] = flip.state
                    if flip.merged_at is not None:
                        facts["merged_at"] = flip.merged_at
                    if flip.closed_at is not None:
                        facts["closed_at"] = flip.closed_at
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
