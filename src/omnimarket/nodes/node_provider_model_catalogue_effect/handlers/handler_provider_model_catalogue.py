# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refuse OpenRouter declarations absent from its provider model catalogue.

Catalogue presence is necessary, not sufficient: an upstream can return 404
even while OpenRouter still advertises the model. This check does not replace
the lab delegation probe. CI reads the public, account-independent catalogue;
operators can supply a captured authenticated response with --catalogue-json.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_provider_model_catalogue_effect.models.model_provider_model_catalogue import (
    ModelProviderModelCatalogueRequest,
    ModelProviderModelCatalogueResult,
)
from omnimarket.routing.byok_model_discovery import get_models_json
from omnimarket.validators.routing_tier_backend_bindability import (
    BIFROST_CONTRACT_PATH,
    declared_backends,
)

CATALOGUE_PATH = BIFROST_CONTRACT_PATH.parent / "byok_provider_backends.v1.yaml"


def catalogue_model_ids(payload: Any) -> frozenset[str]:
    """Fail closed on an empty or malformed catalogue, never infer a pass."""
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("provider catalogue must contain nonempty data[].id")
    ids = []
    for row in rows:
        model = row.get("id") if isinstance(row, dict) else None
        if not isinstance(model, str) or not model.strip():
            raise ValueError("provider catalogue row has no nonempty id")
        ids.append(model)
    return frozenset(ids)


def find_unlisted_models(
    payload: Any,
    *,
    contract_path: Path = BIFROST_CONTRACT_PATH,
    catalogue_path: Path = CATALOGUE_PATH,
) -> dict[str, str]:
    """Check served model_name, including placement peers, and BYOK defaults."""
    ids = catalogue_model_ids(payload)
    missing: dict[str, str] = {}
    for backend_id, backend in declared_backends(contract_path).items():
        if backend.get("provider") != "openrouter":
            continue
        model = backend.get("model_name")
        if not isinstance(model, str) or not model:
            raise ValueError(f"{backend_id} has no model_name")
        if model not in ids:
            missing[backend_id] = model
    catalogue = yaml.safe_load(catalogue_path.read_text(encoding="utf-8"))
    for row in catalogue["providers"]:
        if row["provider"] == "openrouter":
            for model in row["default_models"]:
                if model not in ids:
                    missing[f"{row['backend_id']}:default:{model}"] = model
    return missing


class HandlerProviderModelCatalogue:
    """Read the provider catalogue and judge the declared model ids."""

    def handle(
        self, request: ModelProviderModelCatalogueRequest
    ) -> ModelProviderModelCatalogueResult:
        if request.catalogue_json:
            payload = json.loads(request.catalogue_json.read_text(encoding="utf-8"))
        else:
            catalogue = yaml.safe_load(CATALOGUE_PATH.read_text(encoding="utf-8"))
            row = next(
                r for r in catalogue["providers"] if r["provider"] == "openrouter"
            )
            payload = get_models_json(url=row["models_url"], timeout_seconds=30)
        return ModelProviderModelCatalogueResult(missing=find_unlisted_models(payload))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalogue-json", type=Path)
    args = parser.parse_args(argv)
    try:
        missing = (
            HandlerProviderModelCatalogue()
            .handle(
                ModelProviderModelCatalogueRequest(catalogue_json=args.catalogue_json)
            )
            .missing
        )
    except Exception as exc:
        # Transport exceptions can contain headers. Print only the class name.
        sys.stderr.write(
            f"provider catalogue check could not complete: {type(exc).__name__}\n"
        )
        return 1
    if missing:
        for declaration, model in sorted(missing.items()):
            sys.stderr.write(
                f"provider catalogue does not list {declaration}: {model}\n"
            )
        return 1
    sys.stdout.write("all declared OpenRouter models and defaults are listed\n")
    return 0
