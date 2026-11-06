# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay of recorded request and response pairs against a mounted tenant read.

The recorded set is one JSON document with a ``pairs`` list. Each pair names its
method and path, a masked request and response, and ``masks``: a map of JSON
pointer to the named masking rule applied there. A masked value is replaced by
``<MASK:rule>``; every string is masked, and so are credential, identity,
identifier and volatile scalars. Replay rebuilds each pair's setup from fresh
in-memory state, sends the request to the mounted app and compares the answer
after applying the pair's own masks. Any difference outside those masks fails.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omnimarket.handlers.container.container_tenant_read_dependencies import (
    InMemoryTenantProjectionReader,
)
from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.models.model_tenant_delegation_projection import (
    ModelDelegationRunRow,
    ModelTenantSavingsAggregate,
)
from omnimarket.protocols.protocol_tenant_delegation_projection_reader import (
    ProjectionNotConfiguredError,
    ProjectionUnavailableError,
)
from tests.nodes.tenant_read_support import (
    KEY_A,
    SLUG_A,
    TENANT_A,
    client_for,
    mounted_factory,
)

CORPUS_ENV = "ONEX_T18B_CORPUS_DIR"
SKIP_REASON = (
    f"AC3 (fixture replay) is pending on T18b, OMN-22158: set {CORPUS_ENV} to the recorded "
    "corpus directory; it is unset or holds no pair for this group's identities"
)
UNBOUND_DETAIL = "OIDC authentication is not configured"
NOT_CONFIGURED = "delegation analytics source not configured"
UNAVAILABLE = "delegation analytics source unavailable"


class _ResolvesNothing:
    async def resolve(self, headers: Mapping[str, str]) -> None:
        return None


class _ResolvesTenantA:
    async def resolve(self, headers: Mapping[str, str]) -> ModelHttpTenantIdentity:
        return ModelHttpTenantIdentity(
            tenant_id=TENANT_A, tenant_slug=SLUG_A, principal_id="principal-a"
        )


def corpus_pairs(identities: set[tuple[str, str]]) -> list[dict[str, Any]]:
    """Every recorded pair whose method and path is one of ``identities``."""
    location = os.environ.get(CORPUS_ENV, "").strip()
    if not location:
        return []
    found: list[dict[str, Any]] = []
    for document in sorted(Path(location).rglob("*.json")):
        loaded = json.loads(document.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or not isinstance(loaded.get("pairs"), list):
            continue
        found.extend(
            pair
            for pair in loaded["pairs"]
            if (pair["method"], pair["path"]) in identities
        )
    return found


def _pointer_set(document: Any, pointer: str, value: Any) -> None:
    *parents, last = [
        segment.replace("~1", "/").replace("~0", "~")
        for segment in pointer.lstrip("/").split("/")
    ]
    node = document
    for segment in parents:
        node = node[int(segment)] if isinstance(node, list) else node[segment]
    if isinstance(node, list):
        node[int(last)] = value
    else:
        node[last] = value


def _seed(
    pair: dict[str, Any], path: str
) -> tuple[Any, InMemoryTenantProjectionReader, str]:
    """Identity, reader and query string that rebuild the pair's setup from fresh state."""
    response = pair["response"]
    body = response["body"]
    status = response["status"]
    query = ""
    if status == 401:
        return _ResolvesNothing(), InMemoryTenantProjectionReader(), query
    if status == 503:
        detail = body.get("detail") if isinstance(body, dict) else None
        if detail != NOT_CONFIGURED and detail != UNAVAILABLE:
            return None, InMemoryTenantProjectionReader(), query
        error = (
            ProjectionNotConfiguredError("unset")
            if detail == NOT_CONFIGURED
            else ProjectionUnavailableError("down")
        )
        return (
            _ResolvesTenantA(),
            InMemoryTenantProjectionReader(fail_with=error),
            query,
        )
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    if path.endswith("/savings"):
        reader = InMemoryTenantProjectionReader(
            savings={
                TENANT_A: ModelTenantSavingsAggregate(
                    total_saved_usd=float(body["total_saved_usd"]),
                    total_spend_usd=float(body["total_spend_usd"]),
                    delegation_runs=int(body["delegation_runs"]),
                    savings_latest=stamp,
                )
            }
        )
        return _ResolvesTenantA(), reader, query
    runs = body["runs"]
    rows = tuple(
        ModelDelegationRunRow(
            run_id=f"run-{i}",
            task_type="t",
            model="m",
            quality_gate_passed=True,
            cost_usd=float(run["cost_usd"]),
            saved_usd=float(run["saved_usd"]),
            occurred_at=stamp - timedelta(minutes=i),
        )
        for i, run in enumerate(runs)
    )
    if len(rows) > 5:
        query = f"?limit={len(rows)}"
    return (
        _ResolvesTenantA(),
        InMemoryTenantProjectionReader(runs={TENANT_A: rows}),
        query,
    )


async def replay(node: str, pair: dict[str, Any]) -> list[str]:
    """Replay one pair against the node's mounted app; return every unmasked difference."""
    path = pair["path"]
    identity, reader, query = _seed(pair, path)
    app = mounted_factory(node)(identity=identity, reader=reader)
    async with client_for(app) as client:
        live = await client.get(path + query, headers={"x-api-key": KEY_A})
    differences: list[str] = []
    recorded = pair["response"]
    if live.status_code != recorded["status"]:
        differences.append(f"status {live.status_code} != {recorded['status']}")
    masks = pair.get("masks", {})
    masked_body = _mask_body(live.json(), recorded["body"], masks)
    if masked_body != recorded["body"]:
        differences.append(f"body {masked_body!r} != {recorded['body']!r}")
    return differences


def _mask_body(live: Any, recorded: Any, masks: Mapping[str, str]) -> Any:
    """Mask the live body at exactly the pointers the recorded pair names."""
    wrapper = {"response": {"body": json.loads(json.dumps(live))}}
    for pointer, rule in masks.items():
        if pointer.startswith("/response/body"):
            try:
                _pointer_set(wrapper, pointer, f"<MASK:{rule}>")
            except (KeyError, IndexError, ValueError, TypeError):
                continue
    return wrapper["response"]["body"]
