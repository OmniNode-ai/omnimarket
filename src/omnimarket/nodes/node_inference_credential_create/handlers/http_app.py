# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ASGI factory loaded by the runtime's contract-owned HTTP mount."""

from __future__ import annotations

from importlib.util import find_spec
from pathlib import Path

import yaml
from fastapi import FastAPI

from omnimarket.handlers.inference_credentials_http.app_builder import (
    build_inference_credential_app,
)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
_NODE_PACKAGE_PREFIX = "omnimarket.nodes."


def group_contract_paths() -> list[Path]:
    """The contracts of the nodes this mount serves, as its own contract lists them."""
    own = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    paths: list[Path] = []
    for node in own["http_route_group"]["nodes"]:
        spec = find_spec(_NODE_PACKAGE_PREFIX + node)
        if spec is None or not spec.submodule_search_locations:
            raise ValueError(f"mounted node {node} is not importable")
        paths.append(
            Path(next(iter(spec.submodule_search_locations))) / "contract.yaml"
        )
    return paths


def create_app() -> FastAPI:
    """Build the mount without connecting to a broker or resolving a secret."""
    return build_inference_credential_app(group_contract_paths())
