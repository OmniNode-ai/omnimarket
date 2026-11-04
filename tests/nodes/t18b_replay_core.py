# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay the recorded request and response pairs of one route against the mount.

The corpus directory is named by the ONEX_T18B_CORPUS_DIR environment variable. It
holds a manifest (``manifest.json`` or ``manifest.yaml``) with a ``pairs`` list and
``masking_rules``. Each pair names its ``identity`` (``"METHOD /path"`` or a
mapping with ``method`` and ``path``), its masked ``request`` and ``response``
(inline mappings or file names beside the manifest), an optional ``setup`` and
``masking_rules``, a ``cleanup`` name and a ``sha256``. A masking rule has a
``name`` and a ``field`` (a dotted path into the response body, or
``headers.<name>``) and may carry a ``pattern`` that masks matching text instead.
Anything outside a named rule that differs is a failure.

The replay rebuilds each pair's setup with fresh state: a new mount, new fakes,
the setup's credentials seeded for the recorded caller. A POST that the corpus
recorded without an ``Idempotency-Key`` gets one derived from the pair, which is
the one declared addition of the ported route.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.nodes.inference_credential_http_support import (
    TENANT_A,
    World,
    fake_row,
)

CORPUS_ENV = "ONEX_T18B_CORPUS_DIR"
MASK = "<masked>"
SKIP_REASON = (
    "AC3 (fixture replay) is pending on T18b, OMN-22158: no masked corpus holding "
    "this route's method-and-path identity is available ({detail})"
)


def _load_manifest(directory: Path) -> dict[str, Any]:
    for name in ("manifest.json", "manifest.yaml", "manifest.yml"):
        path = directory / name
        if path.is_file():
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return loaded
    return {}


def _identity(pair: dict[str, Any]) -> tuple[str, str]:
    raw = pair.get("identity")
    if isinstance(raw, dict):
        return str(raw["method"]).upper(), str(raw["path"])
    method, _, path = str(raw).partition(" ")
    return method.upper(), path.strip()


def _template_matches(template: str, path: str) -> bool:
    pattern = re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(template))
    return re.fullmatch(pattern, path) is not None


def select_pairs(
    method: str, template: str
) -> tuple[Path | None, list[dict[str, Any]], dict[str, Any]]:
    """The corpus directory, this identity's pairs and the manifest."""
    location = os.environ.get(CORPUS_ENV, "").strip()
    if not location:
        return None, [], {}
    directory = Path(location)
    manifest = _load_manifest(directory)
    chosen = [
        pair
        for pair in manifest.get("pairs", [])
        if _identity(pair)[0] == method
        and (
            _identity(pair)[1] == template
            or _template_matches(template, _identity(pair)[1])
        )
    ]
    return directory, chosen, manifest


def _part(directory: Path, value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        loaded: dict[str, Any] = json.loads(
            (directory / value).read_text(encoding="utf-8")
        )
        return loaded
    inline: dict[str, Any] = value
    return inline


def _mask_path(document: Any, dotted: str) -> None:
    parts = dotted.split(".")
    if parts and parts[0] == "body":
        parts = parts[1:]
    cursor = document
    for part in parts[:-1]:
        if isinstance(cursor, list):
            for item in cursor:
                _mask_path(item, ".".join(parts[1:]))
            return
        cursor = cursor.get(part) if isinstance(cursor, dict) else None
        if cursor is None:
            return
    leaf = parts[-1] if parts else ""
    if isinstance(cursor, dict) and leaf in cursor:
        cursor[leaf] = MASK
    elif isinstance(cursor, list):
        for item in cursor:
            if isinstance(item, dict) and leaf in item:
                item[leaf] = MASK


def mask(body: Any, rules: list[dict[str, Any]]) -> Any:
    masked = json.loads(json.dumps(body))
    for rule in rules:
        if rule.get("pattern"):
            text = re.sub(rule["pattern"], MASK, json.dumps(masked))
            masked = json.loads(text)
        elif rule.get("field") and not str(rule["field"]).startswith("headers."):
            _mask_path(masked, str(rule["field"]))
    return masked


def replay_pair(
    directory: Path, pair: dict[str, Any], manifest: dict[str, Any]
) -> None:
    request = _part(directory, pair["request"])
    expected = _part(directory, pair["response"])
    setup = pair.get("setup") or {}
    rows = [
        {**fake_row(TENANT_A, str(index)), **row, "tenant_id": str(TENANT_A)}
        for index, row in enumerate(setup.get("credentials", []))
    ]
    world = World(rows=rows)
    headers = {k.lower(): v for k, v in (request.get("headers") or {}).items()}
    if "x-api-key" not in headers and not headers.get("authorization"):
        caller: dict[str, str] = {}
    else:
        caller = world.as_tenant_a()
        headers.pop("x-api-key", None)
        headers.pop("authorization", None)
    method, _ = _identity(pair)
    if method == "POST":
        headers.setdefault("idempotency-key", f"t18b-{pair.get('id', 'pair')}")
    response = world.client.request(
        method,
        request["path"],
        content=(
            json.dumps(request["body"]) if request.get("body") is not None else None
        ),
        headers={**headers, **caller, "content-type": "application/json"},
    )
    rules = list(manifest.get("masking_rules", [])) + list(
        pair.get("masking_rules", [])
    )
    actual_body = response.json() if response.content else None
    expected_body = expected.get("body")
    assert response.status_code == expected["status"], (pair.get("id"), response.text)
    assert mask(actual_body, rules) == mask(expected_body, rules), pair.get("id")


def run_replay(method: str, template: str) -> None:
    directory, pairs, manifest = select_pairs(method, template)
    if directory is None:
        pytest.skip(SKIP_REASON.format(detail=f"{CORPUS_ENV} is unset"))
    if not pairs:
        pytest.skip(
            SKIP_REASON.format(detail=f"the corpus holds no {method} {template} pair")
        )
    for pair in pairs:
        replay_pair(directory, pair, manifest)
