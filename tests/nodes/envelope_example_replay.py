# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay of recorded request/response pairs against the mounted example routes.

The corpus directory comes from one environment variable. Its ``manifest.json`` (or
``manifest.yaml``) holds ``pairs``; each pair carries

* ``identity``: ``"<METHOD> <path>"`` or ``{"method": ..., "path": ...}``,
* ``request`` and ``response``: an inline mapping, or a ``file`` (relative to the
  corpus directory) whose content is checked against the pair's ``sha256``,
* ``masking``: named rules (here or at manifest level) of the response body
  fields, as dotted paths (a string, or a mapping with ``field``/``path``), that
  the recording could not keep stable.

Each pair is replayed against a freshly built app. Any difference outside the
named masking rules fails. The test never passes without pairs: it is skipped,
naming T18b and OMN-22158, only when the corpus location is unset or holds none of
the route's identities. A manifest that names the route's identity but cannot be
read as above fails; it is never skipped.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

from tests.nodes.envelope_example_support import Route

CORPUS_ENV = "ONEX_T18B_CORPUS_DIR"
SKIP_REASON_UNSET = "T18b fixture (OMN-22158) not recorded: set ONEX_T18B_CORPUS_DIR to the corpus directory"
SKIP_REASON_NONE = (
    "T18b corpus (OMN-22158) holds no pair for this route's method-and-path identity"
)


@dataclass(frozen=True)
class Pair:
    method: str
    path: str
    request: dict[str, Any]
    response: dict[str, Any]
    masked: frozenset[str] = field(default_factory=frozenset)


def _identity(raw: Any) -> tuple[str, str]:
    if isinstance(raw, str):
        method, _, path = raw.strip().partition(" ")
        return method.upper(), path.strip()
    if isinstance(raw, dict) and {"method", "path"} <= set(raw):
        return str(raw["method"]).upper(), str(raw["path"])
    raise ValueError(f"unreadable pair identity: {raw!r}")


def _rule_fields(rules: Any) -> set[str]:
    fields: set[str] = set()
    for rule in rules or ():
        if isinstance(rule, str):
            fields.add(rule)
        elif isinstance(rule, dict) and ("field" in rule or "path" in rule):
            fields.add(str(rule.get("field") or rule.get("path")))
        else:
            raise ValueError(f"unreadable masking rule: {rule!r}")
    return fields


def _document(raw: Any, corpus: Path, sha256: str | None) -> dict[str, Any]:
    if isinstance(raw, dict) and "file" not in raw:
        return raw
    if isinstance(raw, dict):
        data = (corpus / str(raw["file"])).read_bytes()
        if sha256 is not None and hashlib.sha256(data).hexdigest() != sha256:
            raise ValueError(f"corpus file {raw['file']} does not match its sha256")
        loaded = yaml.safe_load(data)
        if isinstance(loaded, dict):
            return loaded
    raise ValueError(f"unreadable pair document: {raw!r}")


def load_pairs(corpus: Path, route: Route) -> list[Pair]:
    """Pairs of the corpus whose identity is this route's method and path."""
    manifest_path = next(
        (
            corpus / name
            for name in ("manifest.json", "manifest.yaml", "manifest.yml")
            if (corpus / name).is_file()
        ),
        None,
    )
    if manifest_path is None:
        raise ValueError(f"no manifest in the T18b corpus directory {corpus}")
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("pairs") if isinstance(manifest, dict) else manifest
    if not isinstance(entries, list):
        raise ValueError("the T18b manifest holds no list of pairs")
    shared = (
        _rule_fields(manifest.get("masking")) if isinstance(manifest, dict) else set()
    )
    pairs: list[Pair] = []
    for entry in entries:
        if _identity(entry.get("identity")) != (route.method, route.path):
            continue
        sha = entry.get("sha256")
        pairs.append(
            Pair(
                method=route.method,
                path=route.path,
                request=_document(
                    entry["request"], corpus, sha if isinstance(sha, str) else None
                ),
                response=_document(
                    entry["response"], corpus, sha if isinstance(sha, str) else None
                ),
                masked=frozenset(shared | _rule_fields(entry.get("masking"))),
            )
        )
    return pairs


def corpus_dir() -> Path | None:
    value = os.environ.get(CORPUS_ENV, "").strip()
    return Path(value) if value else None


def _body_of(document: dict[str, Any]) -> Any:
    if "json" in document:
        return document["json"]
    if "body_b64" in document:
        return json.loads(base64.b64decode(document["body_b64"]))
    body = document.get("body")
    if isinstance(body, str):
        return json.loads(body)
    return body


def _mask(body: Any, dotted: str) -> Any:
    head, _, rest = dotted.partition(".")
    if not isinstance(body, dict) or head not in body:
        return body
    copy = dict(body)
    copy[head] = "<masked>" if not rest else _mask(copy[head], rest)
    return copy


def differences(actual_status: int, actual_body: Any, pair: Pair) -> list[str]:
    expected_status = int(pair.response["status"])
    found: list[str] = []
    if actual_status != expected_status:
        found.append(f"status {actual_status} != {expected_status}")
    expected = _body_of(pair.response)
    actual = actual_body
    for dotted in pair.masked:
        expected, actual = _mask(expected, dotted), _mask(actual, dotted)
    if actual != expected:
        keys = (
            sorted(
                k
                for k in set(expected or {}) | set(actual or {})
                if (expected or {}).get(k) != (actual or {}).get(k)
            )
            if isinstance(expected, dict) and isinstance(actual, dict)
            else ["<body>"]
        )
        found.append(f"unmasked body difference in {keys}")
    return found


def replay(route: Route, pair: Pair) -> list[str]:
    """Replay one pair against a freshly built app; the differences found."""
    client = TestClient(route.factory()())
    request = pair.request
    answer = client.request(
        pair.method,
        pair.path,
        headers=request.get("headers") or {},
        content=base64.b64decode(request["body_b64"])
        if "body_b64" in request
        else None,
    )
    try:
        body = answer.json()
    except ValueError:
        body = answer.text
    return differences(answer.status_code, body, pair)
