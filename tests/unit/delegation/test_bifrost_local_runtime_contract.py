# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bifrost delegation local backend render hints."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "configs"
    / "bifrost_delegation.yaml"
)

# OMN-16833: the model registry is the corroborating authority for a backend's
# SERVED model name (`model_name`), distinct from its routing catalog key.
_MODEL_REGISTRY_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "data"
    / "model_registry"
    / "model_registry_v1.yaml"
)


def _model_registry() -> dict[str, object]:
    registry: dict[str, object] = yaml.safe_load(
        _MODEL_REGISTRY_PATH.read_text(encoding="utf-8")
    )
    return registry


@pytest.mark.unit
def test_local_delegation_backends_declare_renderable_endpoint_envs() -> None:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    backends = {backend["backend_id"]: backend for backend in contract["backends"]}

    assert backends["local-coder"]["endpoint_url_env"] == (
        "BIFROST_LOCAL_CODER_ENDPOINT_URL"
    )
    # OMN-16419: was "Qwen3.6-35B-A3B" — repointed to the live-verified SGLang
    # served id at .201:8000 ("qwen3.8"); see bifrost_delegation.yaml.
    assert backends["local-coder"]["model_name"] == "qwen3.8"

    assert backends["local-heavy-reasoning"]["endpoint_url_env"] == (
        "BIFROST_LOCAL_CODER_ENDPOINT_URL"
    )
    assert backends["local-heavy-reasoning"]["model_name"] == "qwen3.8"

    # OMN-16442: `local-reasoner` and `local-coder-mlx` were DELETED from the
    # contract — their endpoints (.201:8001 and .200:8401) were both re-probed
    # 2026-08-28 and return curl exit 7 "Couldn't connect to server", and the
    # canonical inventory (omni_home/docs/reference/AI_LAB_HARDWARE.md,
    # verified 2026-08-28) records .201:8001 as DEAD / RETIRED (RTX 4090 pulled
    # for RMA, OMN-16407). The assertions that pinned their model_names are
    # inverted into absence checks so a revival is caught here.
    for retired in ("local-reasoner", "local-coder-mlx"):
        assert retired not in backends, (
            f"{retired} points at a dead endpoint and must stay deleted; "
            "register the replacement hardware under a new backend_id instead"
        )

    # OMN-16442: model_name was the literal backend_id "local-embedding", not a
    # served id. Live readback 2026-08-28, GET .201:8002/v1/models -> id
    # "text-embedding-qwen3" (vLLM, Qwen/Qwen3-Embedding-0.6B, 1024-dim).
    assert backends["local-embedding"]["model_name"] == "text-embedding-qwen3"

    assert backends["local-ds-v4-flash"]["endpoint_url_env"] == (
        "BIFROST_LOCAL_DS_V4_FLASH_ENDPOINT_URL"
    )
    # OMN-16833: was "ds-v4-flash" — that string is the routing CATALOG KEY, not
    # the served id.  model_registry_v1.yaml's `ds-v4-flash:` entry already
    # declares `model_name: "deepseek-v4-flash"` for this same backend; the
    # bifrost contract had drifted from it.  Live readback 2026-08-28: GET
    # /v1/models on the .200:8101 DS-V4-Flash endpoint lists exactly
    # {"deepseek-v4-flash", "deepseek-v4-pro"}, so "ds-v4-flash" is rejected by
    # the OMN-16419 served-model guard.  Pinned against the registry so the two
    # cannot drift apart again.
    assert backends["local-ds-v4-flash"]["model_name"] == "deepseek-v4-flash"
    assert (
        backends["local-ds-v4-flash"]["model_name"]
        == _model_registry()["models"]["ds-v4-flash"]["model_name"]
    )
