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


@pytest.mark.unit
def test_local_delegation_backends_declare_renderable_endpoint_envs() -> None:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    backends = {backend["backend_id"]: backend for backend in contract["backends"]}

    assert backends["local-coder"]["endpoint_url_env"] == (
        "BIFROST_LOCAL_CODER_ENDPOINT_URL"
    )
    assert backends["local-coder"]["model_name"] == "Qwen3.6-35B-A3B"

    assert backends["local-heavy-reasoning"]["endpoint_url_env"] == (
        "BIFROST_LOCAL_CODER_ENDPOINT_URL"
    )
    assert backends["local-heavy-reasoning"]["model_name"] == "Qwen3.6-35B-A3B"

    assert backends["local-reasoner"]["endpoint_url_env"] == (
        "BIFROST_LOCAL_REASONER_ENDPOINT_URL"
    )
    assert backends["local-reasoner"]["model_name"] == "Qwen3.6-27B-MTP-IQ4_XS.gguf"

    assert backends["local-ds-v4-flash"]["endpoint_url_env"] == (
        "BIFROST_LOCAL_DS_V4_FLASH_ENDPOINT_URL"
    )
    assert backends["local-ds-v4-flash"]["model_name"] == "ds-v4-flash"
