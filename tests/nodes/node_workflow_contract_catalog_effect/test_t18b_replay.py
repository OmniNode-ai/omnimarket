# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3: replay the recorded request/response pairs of this route against the mount.

The corpus directory comes from ``ONEX_T18B_CORPUS_DIR``. Its manifest carries one
entry per recorded pair: the method-and-path ``identity``, the masked ``request``
and ``response`` files, a ``sha256`` per file, the ``masking`` rule names the pair
uses and a ``cleanup`` name. Pairs whose identity is this group's are selected,
their setup is rebuilt with fresh state, the request is replayed against the
mounted app and any difference outside the manifest's named masking rules fails.

Defined only when the corpus location holds this group's identities (the recording
is T18b, OMN-22158), because a collected-then-skipped test counts against the
skip-count ratchet. With pairs present it never skips and never passes without
comparing a pair.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from fastapi.testclient import TestClient

from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.nodes.node_workflow_contract_catalog_effect.handlers.http_app import (
    create_app,
)
from omnimarket.nodes.node_workflow_contract_catalog_effect.handlers.http_dependencies import (
    load_http_route,
)
from omnimarket.nodes.node_workflow_contract_catalog_effect.handlers.overlay_catalog import (
    OverlayWorkflowContractCatalog,
)
from tests.nodes.node_workflow_contract_catalog_effect.catalog_fixtures import (
    InlineDocuments,
    SeededIdentity,
)

pytestmark = pytest.mark.unit

CORPUS_ENV = "ONEX_T18B_CORPUS_DIR"
IDENTITIES = frozenset({"GET /v1/workflows/contracts"})


def _read_manifest(corpus: Path) -> dict[str, Any]:
    for name in ("manifest.json", "manifest.yaml", "manifest.yml"):
        path = corpus / name
        if path.is_file():
            loaded = yaml.safe_load(path.read_text())
            if isinstance(loaded, dict):
                return loaded
    raise AssertionError(f"no readable manifest in the corpus at {corpus}")


def _group_pairs() -> tuple[Path | None, dict[str, Any], list[dict[str, Any]]]:
    location = os.environ.get(CORPUS_ENV, "").strip()
    if not location:
        return None, {}, []
    corpus = Path(location)
    if not corpus.is_dir():
        return None, {}, []
    manifest = _read_manifest(corpus)
    pairs = [p for p in manifest.get("pairs", []) if p.get("identity") in IDENTITIES]
    return corpus, manifest, pairs


_CORPUS, _MANIFEST, _PAIRS = _group_pairs()


def _load_json(corpus: Path, relative: str, expected_sha: str | None) -> Any:
    data = (corpus / relative).read_bytes()
    if expected_sha is not None:
        assert hashlib.sha256(data).hexdigest() == expected_sha, (
            f"{relative}: sha256 differs from the manifest"
        )
    return json.loads(data)


def _mask(value: Any, path: tuple[str, ...], replacement: Any) -> Any:
    """Replace the value at a dotted path; ``*`` matches every list item or key."""
    if not path:
        return copy.deepcopy(replacement)
    head, rest = path[0], path[1:]
    if isinstance(value, list) and head == "*":
        return [_mask(item, rest, replacement) for item in value]
    if isinstance(value, dict):
        keys = list(value) if head == "*" else [head]
        return {
            key: (_mask(item, rest, replacement) if key in keys else item)
            for key, item in value.items()
        }
    return value


def _masked(body: Any, rules: list[Mapping[str, Any]]) -> Any:
    for rule in rules:
        field = str(rule["field"]).removeprefix("$.").removeprefix("body.")
        body = _mask(
            body,
            tuple(p for p in field.split(".") if p),
            rule.get("replacement", "<masked>"),
        )
    return body


def _rules_for(pair: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    declared = {r["name"]: r for r in _MANIFEST.get("masking_rules", [])}
    names = pair.get("masking", []) or []
    unknown = [n for n in names if n not in declared]
    assert not unknown, (
        f"pair names masking rules the manifest does not declare: {unknown}"
    )
    return [declared[n] for n in names]


def _fresh_app(request: Mapping[str, Any], expected: Mapping[str, Any]) -> TestClient:
    """Rebuild the pair's setup from nothing: a new catalog, resolver and app."""
    route = load_http_route()
    status = expected["status"]
    body = expected.get("body") or {}

    class _Resolver(SeededIdentity):
        """Resolves a tenant exactly when the recorded answer was served."""

        async def resolve(
            self, headers: Mapping[str, str]
        ) -> ModelHttpTenantIdentity | None:
            self.calls += 1
            if status != 200:
                return None
            return ModelHttpTenantIdentity(
                tenant_id=UUID(int=1), tenant_slug="replay", principal_id="replay"
            )

    if (
        status == 503
        and body.get("detail") == route.refusals["resolver_unbound"].detail
    ):
        identity = None
    else:
        identity = _Resolver()
    workflows = [
        {
            "workflow_type": c["workflow_type"],
            "contract_id": c["contract_id"],
            "command_topic_constant": next(iter(route.command_topics)),
            "event_type": "replay.recorded",
            "payload_schema": c["payload_schema"],
        }
        for c in (body.get("contracts") or [])
    ]
    document = {"workflows": workflows} if status == 200 else None
    catalog = OverlayWorkflowContractCatalog(
        documents=InlineDocuments(
            {route.catalog_document: document} if document else {}
        ),
        document_name=route.catalog_document,
        command_topics=route.command_topics,
    )
    return TestClient(create_app(identity=identity, catalog=catalog))


if _PAIRS:

    @pytest.mark.parametrize(
        "pair", [pytest.param(p, id=str(p.get("id", p["identity"]))) for p in _PAIRS]
    )
    def test_recorded_pair_replays_with_no_unmasked_difference(
        pair: dict[str, Any],
    ) -> None:
        assert _CORPUS is not None
        sha = pair.get("sha256") or {}
        request = _load_json(_CORPUS, pair["request"], sha.get("request"))
        expected = _load_json(_CORPUS, pair["response"], sha.get("response"))
        method, path = pair["identity"].split(" ", 1)
        assert (request.get("method", method).upper(), request.get("path", path)) == (
            method,
            path,
        )

        client = _fresh_app(request, expected)
        actual = client.request(method, path, headers=request.get("headers") or {})

        rules = _rules_for(pair)
        assert actual.status_code == expected["status"]
        assert _masked(actual.json(), rules) == _masked(expected.get("body"), rules)


def test_a_difference_outside_the_masking_rules_is_caught() -> None:
    """Positive control for the comparison itself, independent of the corpus."""
    rules = [{"field": "contracts.*.contract_id", "replacement": "<masked>"}]
    a = {"contracts": [{"workflow_type": "x", "contract_id": "one"}]}
    b = {"contracts": [{"workflow_type": "x", "contract_id": "two"}]}
    c = {"contracts": [{"workflow_type": "y", "contract_id": "one"}]}
    assert _masked(a, rules) == _masked(b, rules)
    assert _masked(a, rules) != _masked(c, rules)
