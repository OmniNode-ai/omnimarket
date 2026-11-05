# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay engine for the T18b corpus subset of this route.

Coded against the manifest fields the recorder lane names: a manifest with an
identity, masking rules, a cleanup name and a sha256 per pair, and one masked
request and response pair per method-and-path identity. Field spellings are read
tolerantly (JSON or YAML manifest; a pair's request/response inline or as files
relative to the corpus), and a pair that cannot be read fails; it is never
skipped. Each pair is replayed against a freshly built app with state rebuilt
from the pair alone, then compared after applying the corpus's named masks.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

from omnimarket.nodes.node_hook_events_by_correlation_read_effect.handlers.http_app import (
    create_app,
)
from omnimarket.nodes.node_hook_events_by_correlation_read_effect.protocols.protocol_hook_events_reader import (
    ProjectionAbsentError,
    ProjectionNotConfiguredError,
    ProjectionUnavailableError,
)
from tests.nodes.node_hook_events_by_correlation_read_effect.support import (
    FakeIdentity,
    FakeReader,
)

IDENTITY = ("GET", "/v1/projections/hook-events/by-correlation")
MANIFEST_NAMES = ("manifest.json", "manifest.yaml", "manifest.yml")
SKIP_REASON = (
    "AC3 (fixture replay) is pending on T18b, OMN-22158: the corpus location is "
    "unset or holds none of this route's identities"
)


@dataclass(frozen=True)
class Pair:
    identity: tuple[str, str]
    request: dict[str, Any]
    response: dict[str, Any]


def _identity(raw: Any) -> tuple[str, str]:
    if isinstance(raw, Mapping):
        return (str(raw["method"]).upper(), str(raw["path"]))
    method, _, path = str(raw).strip().partition(" ")
    return (method.upper(), path.strip())


def _load_part(corpus: Path, value: Any, expected_sha: str | None) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    raw = (corpus / str(value)).read_bytes()
    if expected_sha is not None and hashlib.sha256(raw).hexdigest() != expected_sha:
        raise AssertionError(f"sha256 mismatch for corpus file {value}")
    loaded = yaml.safe_load(raw)
    assert isinstance(loaded, dict), f"corpus file {value} is not a mapping"
    return loaded


def load_manifest(corpus: Path) -> dict[str, Any]:
    for name in MANIFEST_NAMES:
        path = corpus / name
        if path.is_file():
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            assert isinstance(loaded, dict), "corpus manifest is not a mapping"
            return loaded
    raise AssertionError(f"no manifest ({', '.join(MANIFEST_NAMES)}) in the corpus")


def select_pairs(corpus: Path) -> tuple[list[Pair], list[dict[str, Any]]]:
    """This route's pairs and the corpus's named masking rules."""
    manifest = load_manifest(corpus)
    rules = list(manifest.get("masking_rules") or manifest.get("masking") or [])
    pairs: list[Pair] = []
    for entry in manifest.get("pairs") or []:
        identity = _identity(entry["identity"])
        if identity != IDENTITY:
            continue
        sha = entry.get("sha256")
        shas = sha if isinstance(sha, Mapping) else {}
        pairs.append(
            Pair(
                identity=identity,
                request=_load_part(corpus, entry["request"], shas.get("request")),
                response=_load_part(corpus, entry["response"], shas.get("response")),
            )
        )
    return pairs, rules


def _mask(node: Any, parts: list[str], token: str) -> Any:
    if not parts:
        return token
    head, rest = parts[0], parts[1:]
    if head == "*" and isinstance(node, list):
        return [_mask(item, rest, token) for item in node]
    if isinstance(node, dict) and head in node:
        return {**node, head: _mask(node[head], rest, token)}
    return node


def apply_masks(body: Any, rules: list[dict[str, Any]]) -> Any:
    for rule in rules:
        path = str(rule.get("path") or rule.get("pointer") or "").removeprefix("$.")
        parts = [p for p in path.replace("[*]", ".*").split(".") if p]
        body = _mask(body, parts, f"<masked:{rule.get('name', path)}>")
    return body


def _state_for(response: Mapping[str, Any]) -> tuple[FakeReader, FakeIdentity | None]:
    """Rebuild the repository and identity a recorded response implies."""
    status = int(response["status"])
    body = response.get("body") or {}
    if status == 503 and body.get("detail") == "OIDC authentication is not configured":
        return FakeReader(), None
    if status == 401:
        return FakeReader(), FakeIdentity({})
    if status == 503:
        configured = "not configured" in str(body.get("detail"))
        exc: Exception = (
            ProjectionNotConfiguredError("replay")
            if configured
            else ProjectionUnavailableError("replay")
        )
        return FakeReader(raises=exc), FakeIdentity()
    scope = body["tenant_scope"]
    if body["data_state"] == "projection_absent":
        return FakeReader(raises=ProjectionAbsentError("replay")), FakeIdentity()
    rows = [
        {
            "event_sha": "0" * 64,
            **r,
            "tenant_id": r.get("tenant_id", scope),
            "payload": r.get("payload") or {},
        }
        for r in body.get("rows", [])
    ]
    return FakeReader(rows), FakeIdentity()


async def replay(pair: Pair) -> tuple[int, Any]:
    reader, identity = _state_for(pair.response)
    request = pair.request
    headers = {
        str(k).lower(): str(v) for k, v in (request.get("headers") or {}).items()
    }
    if identity is not None and pair.response["status"] != 401:
        body = pair.response.get("body") or {}
        key = headers.get("x-api-key", "replay-key")
        headers["x-api-key"] = key
        identity.keys[key] = body.get("tenant_scope", "replay-tenant")
    app = (
        create_app(reader=reader, identity=identity)
        if identity
        else create_app(reader=reader)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://replay.invalid"
    ) as client:
        resp = await client.request(
            request.get("method", "GET"),
            request["path"],
            params=request.get("query") or request.get("params") or None,
            headers=headers,
        )
    return resp.status_code, resp.json()


def difference(
    pair: Pair, status: int, body: Any, rules: list[dict[str, Any]]
) -> list[str]:
    problems: list[str] = []
    if status != int(pair.response["status"]):
        problems.append(f"status {status} != recorded {pair.response['status']}")
    expected = apply_masks(pair.response.get("body"), rules)
    actual = apply_masks(body, rules)
    if expected != actual:
        problems.append(f"body differs after masking: {actual!r} != {expected!r}")
    return problems


async def replay_all(corpus: Path) -> tuple[int, list[str]]:
    pairs, rules = select_pairs(corpus)
    problems: list[str] = []
    for pair in pairs:
        status, body = await replay(pair)
        problems += [
            f"{pair.request.get('query')}: {p}"
            for p in difference(pair, status, body, rules)
        ]
    return len(pairs), problems


def dump(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True)
