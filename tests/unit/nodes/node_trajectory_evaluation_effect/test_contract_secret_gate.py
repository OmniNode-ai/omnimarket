# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime wires the node on a lane that holds no Dharma secret (OMN-20090).

The runtime's auto-wiring drops every contract that marks a secret
``required: true`` when its store does not hold that secret (OMN-21423,
``omnibase_infra.runtime.auto_wiring.discovery._extract_required_secrets``
reads exactly ``declaration.get("required") is True``). The Dharma secrets are
read only under the ``dharma_full`` setting, so a lane without them (``off`` or
``in_memory``) must still join the node's consumer group; the handler refuses
``dharma_full`` without them as ``backend_misconfigured`` instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_trajectory_evaluation_effect as node_pkg
from omnimarket.nodes.node_trajectory_evaluation_effect.models.model_trajectory_evaluation import (
    ModelTrajectoryEvaluationRefused,
)
from tests.unit.nodes.node_trajectory_evaluation_effect.fakes import (
    full_cmd,
    make_handler,
)

pytestmark = pytest.mark.unit

CONTRACT = Path(node_pkg.__file__).parent / "contract.yaml"


def test_contract_marks_no_secret_required() -> None:
    secrets = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["secrets"]
    assert set(secrets) == {"dharma.api_key", "dharma.org_id"}
    required = sorted(
        name
        for name, declaration in secrets.items()
        if declaration.get("required") is True
    )
    assert required == []


async def test_dharma_full_without_secrets_refused_while_no_secret_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relaxing required must not let dharma_full run unguarded."""
    secrets = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["secrets"]
    required = sorted(
        name
        for name, declaration in secrets.items()
        if declaration.get("required") is True
    )
    assert required == []

    for name in ("DHARMA_API_KEY", "DHARMA_ORG_ID", "ONEX_SECRET_RESOLVER_CONFIG_PATH"):
        monkeypatch.delenv(name, raising=False)
    handler, github, _, _ = make_handler("dharma_full")
    out = await handler.handle(full_cmd())
    assert isinstance(out, ModelTrajectoryEvaluationRefused)
    assert out.reason == "backend_misconfigured"
    assert "DHARMA" not in out.detail
    assert github.requests == []
