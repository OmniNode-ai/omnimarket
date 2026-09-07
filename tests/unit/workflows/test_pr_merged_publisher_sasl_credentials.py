# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pr-merged publisher job must carry the dev-lane SCRAM principal (OMN-18012).

``config/ci_bus_lanes.yaml`` declares the dev lane as ``SASL_PLAINTEXT`` /
``SCRAM-SHA-256``, and ``publish_pr_merged_event.py`` reads that declaration and
refuses to downgrade it. A job that injects no credentials therefore cannot
publish at all — which is exactly what happened from ~18:14Z on 2026-09-07:
every "Publish pr-merged event" run reded with a delivery timeout on an
unpublished event, because the workflow supplied no principal while the listener
had begun demanding one.

The broker address is deliberately NOT a secret here (OMN-14800/OMN-17378): it
is config, declared in the same committed overlay, and an opaque injection is
what let a silent lane repoint run green. Config and credential are different
things and this test pins both halves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

_WORKFLOW = (
    Path(__file__).resolve().parents[3]
    / ".github"
    / "workflows"
    / "pr-merged-publisher.yml"
)


def _publish_step_env() -> dict[str, str]:
    loaded = cast(
        "dict[str, Any]", yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    )
    steps = loaded["jobs"]["publish-pr-merged"]["steps"]
    publish = next(
        step
        for step in steps
        if "publish_pr_merged_event.py" in str(step.get("run", ""))
    )
    return cast("dict[str, str]", publish.get("env", {}))


@pytest.mark.unit
def test_publish_step_injects_the_sasl_principal() -> None:
    """Both halves of the SCRAM credential reach the publisher's environment."""
    env = _publish_step_env()

    assert env["KAFKA_SASL_USERNAME"] == "${{ secrets.KAFKA_SASL_USERNAME }}"
    assert env["KAFKA_SASL_PASSWORD"] == "${{ secrets.KAFKA_SASL_PASSWORD }}"


@pytest.mark.unit
def test_publish_step_does_not_inject_the_broker_address() -> None:
    """The broker stays overlay-resolved config, never an opaque secret."""
    assert "KAFKA_BOOTSTRAP_SERVERS" not in _publish_step_env()


@pytest.mark.unit
def test_publish_step_still_selects_the_declared_lane() -> None:
    """The credential is useless without the lane whose transport declares it."""
    raw = _WORKFLOW.read_text(encoding="utf-8")

    assert "--lane dev" in raw
