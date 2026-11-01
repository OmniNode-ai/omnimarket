# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delete long-Empty per-call consumer groups from a bus, under the limits the node contract declares (OMN-21818).

The broker keeps an Empty group for 24 hours and replays every one at start, so per-call
groups left by short-lived clients are deleted here instead of waiting for the guard's
threshold alarm. Safety rules, all read from ``contract.yaml``:

* a group is a candidate only when it has been Empty in every run for at least
  ``min_empty_age_seconds`` (two listings that far apart agree); the first-seen times are
  kept in ``empty_since.json`` in the tick's state directory;
* its name must match a ``reapable_patterns`` regex and no ``protected_patterns`` regex
  (protected wins), so a stable-named group is never deleted;
* at most ``max_deletes_per_run`` groups per run, in batches of ``batch_size``;
* the broker is checked after every batch, and the run stops at the first delete error or
  unhealthy reading. The broker itself refuses to delete a group that has members.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable, Mapping
from importlib import resources
from pathlib import Path

import yaml

from omnimarket.models.consumer_group_guard import (
    ModelConsumerGroupReapRequest,
    ModelConsumerGroupReapResult,
    ModelReapRules,
)
from omnimarket.nodes.node_runtime_coverage_effect.handlers.handler_runtime_coverage import (
    resolve_broker,
)

_TIMEOUT = 30.0
_NODE = "node_consumer_group_guard_effect"
_STATE_FILE = "empty_since.json"
# Outcomes that mean the group changed under us rather than that the broker is unwell.
_BENIGN_ERRORS = ("GROUP_ID_NOT_FOUND", "NON_EMPTY_GROUP")

GroupReader = Callable[[Mapping[str, str]], dict[str, str]]
GroupDeleter = Callable[[Mapping[str, str], list[str]], dict[str, str]]
HealthChecker = Callable[[Mapping[str, str]], str]


def contract_reap_rules() -> ModelReapRules:
    contract = yaml.safe_load(
        resources.files(f"omnimarket.nodes.{_NODE}")
        .joinpath("contract.yaml")
        .read_text()
    )
    return ModelReapRules.model_validate(contract["consumer_group_guard"]["reap"])


def is_reapable(name: str, rules: ModelReapRules) -> bool:
    if any(re.search(p, name) for p in rules.protected_patterns):
        return False
    return any(re.search(p, name) for p in rules.reapable_patterns)


def read_groups(client_config: Mapping[str, str]) -> dict[str, str]:
    """Admin-only listing of group name to upper-case state."""
    from confluent_kafka.admin import AdminClient

    admin = AdminClient(dict(client_config))
    listing = admin.list_consumer_groups(request_timeout=_TIMEOUT).result(
        timeout=_TIMEOUT
    )
    if listing.errors:
        raise RuntimeError("broker group listing incomplete")
    return {
        g.group_id: str(getattr(g.state, "name", g.state)).rsplit(".", 1)[-1].upper()
        for g in listing.valid
    }


def delete_groups(client_config: Mapping[str, str], names: list[str]) -> dict[str, str]:
    """Delete the groups; the value is "" on success or the error name."""
    from confluent_kafka.admin import AdminClient

    admin = AdminClient(dict(client_config))
    futures = admin.delete_consumer_groups(names, request_timeout=_TIMEOUT)
    outcome: dict[str, str] = {}
    for name, future in futures.items():
        try:
            future.result(timeout=_TIMEOUT)
            outcome[name] = ""
        except Exception as exc:
            error = getattr(exc, "args", [None])[0]
            code = getattr(error, "name", None)
            outcome[name] = (callable(code) and str(code())) or type(exc).__name__
    return outcome


def check_health(client_config: Mapping[str, str]) -> str:
    """ "" when the broker answers a cluster and a group listing, else the reason."""
    from confluent_kafka.admin import AdminClient

    try:
        admin = AdminClient(dict(client_config))
        cluster = admin.describe_cluster(request_timeout=_TIMEOUT).result(
            timeout=_TIMEOUT
        )
        if not cluster.nodes:
            return "no brokers in the cluster description"
        listing = admin.list_consumer_groups(request_timeout=_TIMEOUT).result(
            timeout=_TIMEOUT
        )
        if listing.errors:
            return "group listing incomplete"
    except Exception as exc:
        return f"broker unreadable: {type(exc).__name__}"
    return ""


def _load_first_seen(state_dir: Path) -> dict[str, float]:
    try:
        raw = json.loads((state_dir / _STATE_FILE).read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in raw.items()}
    except (OSError, ValueError, AttributeError, TypeError):
        return {}


def _save_first_seen(state_dir: Path, first_seen: Mapping[str, float]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / f".{_STATE_FILE}.tmp"
    tmp.write_text(json.dumps(first_seen), encoding="utf-8")
    tmp.replace(state_dir / _STATE_FILE)


class HandlerConsumerGroupReaper:
    def __init__(
        self,
        group_reader: GroupReader | None = None,
        group_deleter: GroupDeleter | None = None,
        health_checker: HealthChecker | None = None,
        rules_reader: Callable[[], ModelReapRules] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._read = group_reader or read_groups
        self._delete = group_deleter or delete_groups
        self._health = health_checker or check_health
        self._rules = rules_reader or contract_reap_rules
        self._clock = clock

    async def handle(
        self, request: ModelConsumerGroupReapRequest
    ) -> ModelConsumerGroupReapResult:
        try:
            _broker, _environment, config = resolve_broker(request.bus_lane)
            deleted, detail = await asyncio.to_thread(
                self.reap, config, Path(request.state_dir)
            )
        except Exception as exc:
            deleted, detail = 0, f"reap skipped: {type(exc).__name__}"
        return ModelConsumerGroupReapResult(
            bus_lane=request.bus_lane, groups_reaped=deleted, detail=detail
        )

    def reap(self, config: Mapping[str, str], state_dir: Path) -> tuple[int, str]:
        """Return (groups deleted, detail). Never raises: a failure is the detail."""
        rules = self._rules()
        if not rules.enabled:
            return 0, "reaping disabled by the contract"
        try:
            states = self._read(config)
        except Exception as exc:
            return 0, f"reap skipped, listing unreadable: {type(exc).__name__}"
        now = self._clock()
        previous = _load_first_seen(state_dir)
        empty = sorted(n for n, s in states.items() if s == "EMPTY")
        # A group that left Empty, or vanished, drops out and starts over if it returns.
        first_seen = {n: previous.get(n, now) for n in empty}
        _save_first_seen(state_dir, first_seen)
        eligible = [
            n
            for n in empty
            if now - first_seen[n] >= rules.min_empty_age_seconds
            and is_reapable(n, rules)
        ][: rules.max_deletes_per_run]
        if not eligible:
            return 0, f"nothing to reap among {len(empty)} Empty groups"
        reason = self._health(config)
        if reason:
            return (
                0,
                f"reap skipped, broker not healthy before the first batch: {reason}",
            )
        deleted = 0
        for start in range(0, len(eligible), rules.batch_size):
            batch = eligible[start : start + rules.batch_size]
            try:
                outcome = self._delete(config, batch)
            except Exception as exc:
                return (
                    deleted,
                    f"reap stopped, delete call failed: {type(exc).__name__}",
                )
            deleted += sum(1 for v in outcome.values() if v == "")
            errors = sorted(
                {v for v in outcome.values() if v not in ("", *_BENIGN_ERRORS)}
            )
            if errors:
                return deleted, f"reap stopped at the first error: {', '.join(errors)}"
            reason = self._health(config)
            if reason:
                return (
                    deleted,
                    f"reap stopped, broker not healthy after a batch: {reason}",
                )
        return deleted, f"reaped {deleted} of {len(eligible)} eligible Empty groups"
